"""The impairment harness must be able to say PASS or FAIL, not just print
numbers (#63).

WHY THIS FILE EXISTS
--------------------
`tools/loopback_throughput.py --mode impair` already measures a seeded,
repeatable-enough soak (#51, #81) with rich recovery counters - but every
existing caller reads the printed report and judges it by eye. #63 asked for
"a numeric stream-delivery verdict" a script can act on without a human
reading the output, and for the report to separate WHY delivery suffered
(packet loss, admission withdrawal, reorder-deadline expiry) rather than
leaving all three folded into one delivered_pct.

`--min-delivered-pct` and `delivery_breakdown` are what this file pins.
Deliberately margins wide enough that neither can be flipped by the harness's
own documented timing noise (its module docstring: end-to-end counters over
real sockets and a real clock are "NOT bit-reproducible") - a clean bond
against a lenient bar, a heavily-lossy single leg against a strict one,
nothing in between.
"""
from __future__ import annotations

from tools.loopback_throughput import main

SEED = 90210


def test_a_clean_bond_passes_a_lenient_threshold() -> None:
    rc = main([
        "--mode", "impair", "--legs", "2", "--payload", "200",
        "--impair-legs", "", "--payloads", "1500", "--offered-pps", "500",
        "--seed", str(SEED), "--min-delivered-pct", "99.0", "--json",
    ])
    assert rc == 0, "a clean two-leg bond must clear a 99% bar"


def test_a_heavily_lossy_single_leg_fails_a_strict_threshold() -> None:
    """THE ONE THAT MATTERS. Before --min-delivered-pct existed, this exact
    run printed its numbers and returned 0 regardless - a script driving it
    could not tell a bad run from a good one without parsing prose."""
    rc = main([
        "--mode", "impair", "--legs", "1", "--payload", "200",
        "--impair-legs", "0", "--impair-loss", "0.4",
        "--payloads", "1200", "--offered-pps", "500",
        "--seed", str(SEED), "--min-delivered-pct", "99.0", "--json",
    ])
    assert rc == 1, "a single leg losing 40% of its datagrams must fail a 99% bar"


def test_omitting_the_threshold_keeps_the_old_always_zero_behaviour() -> None:
    """A caller that never asks for a verdict must never start receiving a
    nonzero exit from a tool that always returned 0 - the same lossy scenario
    above, with no --min-delivered-pct, must still return 0."""
    rc = main([
        "--mode", "impair", "--legs", "1", "--payload", "200",
        "--impair-legs", "0", "--impair-loss", "0.4",
        "--payloads", "1200", "--offered-pps", "500",
        "--seed", str(SEED),
    ])
    assert rc == 0


def test_json_output_is_a_bare_list_without_a_verdict_requested(capsys) -> None:
    """Existing callers of --json get exactly what they always got. The
    wrapped {"rows": ..., "verdict": ...} shape is additive, gated on
    --min-delivered-pct actually being passed."""
    import json

    main([
        "--mode", "impair", "--legs", "2", "--payload", "200",
        "--impair-legs", "", "--payloads", "500", "--offered-pps", "500",
        "--seed", str(SEED), "--json",
    ])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert isinstance(parsed, list), (
        "adding a verdict feature changed the JSON shape for callers who "
        "never asked for one"
    )


def test_json_output_wraps_rows_and_verdict_together_when_requested() -> None:
    import contextlib
    import io
    import json

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exit_code = main([
            "--mode", "impair", "--legs", "2", "--payload", "200",
            "--impair-legs", "", "--payloads", "500", "--offered-pps", "500",
            "--seed", str(SEED), "--min-delivered-pct", "50.0", "--json",
        ])
    parsed = json.loads(buf.getvalue())
    assert exit_code == 0
    assert set(parsed.keys()) == {"rows", "verdict"}
    assert parsed["verdict"]["passed"] is True
    assert parsed["verdict"]["min_delivered_pct"] == 50.0


def test_the_breakdown_separates_loss_from_reorder_deadline_expiry() -> None:
    """#81's own case, replayed through the real harness: a bufferbloated
    leg beside a healthy one must show up as reorder-deadline-expired, NOT
    as loss - the incident's own numbers were zero packet loss.

    NEEDS TWO LEGS AND A PAYLOAD OVER THE DUPLICATE THRESHOLD (250 bytes,
    --no-duplicate here to be explicit). A single delayed leg has nothing to
    be a gap RELATIVE TO - every packet still arrives in its own order, just
    late - and a small payload gets copied onto every leg by the classifier
    (#51), so the fast leg's copy already closes the gap before the slow
    one's copy ever needs a NACK. Caught by running this against a 200-byte,
    single-leg version first: reorder_deadline_expired_pct came back 0.0 for
    a reason that had nothing to do with the breakdown code being wrong.
    """
    import contextlib
    import io
    import json

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        main([
            "--mode", "impair", "--legs", "2", "--payload", "1263",
            "--no-duplicate",
            "--impair-legs", "1", "--impair-delay-ms", "400",
            "--reorder-deadline-ms", "250",
            "--payloads", "800", "--offered-pps", "300",
            "--seed", str(SEED), "--json",
        ])
    row = json.loads(buf.getvalue())[0]
    brk = row["delivery_breakdown"]
    assert brk["loss_pct"] < 5.0, (
        f"a pure-delay impairment reported {brk['loss_pct']}% loss - the "
        f"breakdown is not actually separating the two axes"
    )
    assert brk["reorder_deadline_expired_pct"] > 0.0, (
        "a 400ms delay against a 250ms reorder deadline, beside a healthy "
        "leg, produced no reorder-deadline-expired signal at all"
    )


def test_worst_leg_withdrawn_is_none_without_the_policy_control() -> None:
    """shed mode pins every leg UP by design (see run_impaired's own
    docstring) - reporting a withdrawn percentage there would be reporting
    the harness's assumption dressed as a measurement, so this must stay
    None rather than a fabricated 0."""
    import contextlib
    import io
    import json

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        main([
            "--mode", "impair", "--legs", "2", "--payload", "200",
            "--impair-legs", "", "--payloads", "300", "--offered-pps", "500",
            "--seed", str(SEED), "--json",
        ])
    row = json.loads(buf.getvalue())[0]
    assert row["delivery_breakdown"]["worst_leg_withdrawn_pct"] is None
