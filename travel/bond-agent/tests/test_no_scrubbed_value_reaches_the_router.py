"""The repo is public and describes a private router, so it lies about identity.

On 2026-08-29 one of those lies reached the travel router. `server_public_key` arrived as the
literal `<server-public-key>`, `wg setconf` rejected it, the bond never came up,
and because zippie owns the router's only default route the box left the network.
Recovery took physical access.

The fix for that caught one field. The same bug was still armed one field over,
and was found by dry-running the deploy against the router on the same day: the
repo also ships `endpoint = "dns-e.example-home.invalid"` and a `lan_endpoints`
block on 192.0.2.0/24. `.invalid` can never resolve (RFC 2606) and 192.0.2.0/24
is documentation space (RFC 5737), so a deploy would have pointed the bond at a
name with no answer - the identical outage, and `deploy.travel-router.yml` runs on every
push to main that touches `travel/`.

The previous guard could not have caught it. It looked for the SHAPE of one
placeholder, `= "<...>"`, and `"dns-e.example-home.invalid"` is not that shape.
It is not a placeholder at all. It is a scrub, and a scrub parses perfectly.

These tests run the deploy script's OWN renderer - extracted from the script, not
reimplemented - against fixtures, so what is asserted is what ships.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DEPLOY = REPO_ROOT / "scripts" / "deploy-openwrt.sh"
LIVE_CONFIG_IN_REPO = REPO_ROOT / "travel" / "gl-mt3000" / "zippie.toml"

REAL_KEY = "cMkhUCDaGTjInWtCEG8TpMRo40f5BimY1IWKZ18rmmc="


@pytest.fixture(scope="module")
def renderer(tmp_path_factory) -> Path:
    """The renderer as the script actually runs it.

    EXTRACTED RATHER THAN REIMPLEMENTED. A copy of this logic in the test suite
    would pass forever while the script shipped something else - which is the
    exact failure mode ("the file is documentation of an intention, and nothing
    said so") that made the config a deployed artifact in the first place.
    """
    text = DEPLOY.read_text()
    body = text.split(
        'python3 - "${CONFIG_SRC}" "${LIVE_CONFIG}" "${RENDERED_CONFIG}" <<\'RENDER\'\n', 1
    )
    assert len(body) == 2, "the config renderer is no longer a RENDER heredoc"
    script = body[1].split("\nRENDER\n", 1)[0]
    out = tmp_path_factory.mktemp("renderer") / "render.py"
    out.write_text(script)
    return out


def render(renderer: Path, repo: str, live: str, tmp_path: Path):
    """Run it. Returns (returncode, stdout+stderr, rendered text or None)."""
    repo_file = tmp_path / "repo.toml"
    live_file = tmp_path / "live.toml"
    out_file = tmp_path / "out.toml"
    repo_file.write_text(repo)
    live_file.write_text(live)
    done = subprocess.run(
        [sys.executable, str(renderer), str(repo_file), str(live_file), str(out_file)],
        capture_output=True, text=True, check=False,
    )
    rendered = out_file.read_text() if out_file.exists() else None
    return done.returncode, done.stdout + done.stderr, rendered


LIVE = f"""[home]
endpoint = "dns-e.realhome.net"
server_public_key = "{REAL_KEY}"
"""


def test_a_scrubbed_endpoint_is_replaced_by_the_router_s_own(renderer, tmp_path):
    """THE ONE THAT WAS STILL ARMED. `.invalid` is reserved by RFC 2606 and can
    never resolve, so a bond pointed at it can never form."""
    repo = '[home]\nendpoint = "dns-e.example-home.invalid"\n' \
           f'server_public_key = "{REAL_KEY}"\n'
    code, output, rendered = render(renderer, repo, LIVE, tmp_path)
    assert code == 0, output
    assert 'endpoint = "dns-e.realhome.net"' in rendered
    assert "invalid" not in rendered


def test_a_scrubbed_key_is_still_replaced(renderer, tmp_path):
    """The 2026-08-29 field itself, kept working by the generalised path."""
    repo = '[home]\nendpoint = "dns-e.realhome.net"\n' \
           'server_public_key = "<server-public-key>"\n'
    code, output, rendered = render(renderer, repo, LIVE, tmp_path)
    assert code == 0, output
    assert f'server_public_key = "{REAL_KEY}"' in rendered


def test_a_scrubbed_field_the_router_cannot_supply_stops_the_deploy(renderer, tmp_path):
    """A router with nothing to preserve must not be deployed to.

    The alternative is shipping the scrub, which is the outage. Refusing costs a
    red build; shipping costs physical access to a router in a hotel.
    """
    repo = '[home]\nendpoint = "dns-e.example-home.invalid"\n'
    code, output, _ = render(renderer, repo, "[home]\n", tmp_path)
    assert code != 0
    assert "no real value to preserve" in output
    assert "NOTHING was sent" in output


def test_a_router_holding_its_own_scrub_is_not_treated_as_a_real_value(
    renderer, tmp_path
):
    """The router's copy is only useful if it is REAL.

    A router that was already deployed to with a scrubbed value would otherwise
    have that value read back and re-rendered as though it were the truth -
    laundering the outage into a config that looks preserved.
    """
    repo = '[home]\nendpoint = "dns-e.example-home.invalid"\n'
    live = '[home]\nendpoint = "other.example-home.invalid"\n'
    code, output, _ = render(renderer, repo, live, tmp_path)
    assert code != 0
    assert "no real value to preserve" in output


def test_a_documentation_lan_endpoints_block_is_removed_not_shipped(
    renderer, tmp_path
):
    """OPTIONAL, so removal restores exactly today's behaviour.

    Shipping 192.0.2.0/24 would leave a matcher that matches nothing while
    reading, to anybody looking, as configured - which is worse than absent.
    """
    repo = (
        '[home]\nendpoint = "dns-e.realhome.net"\n'
        f'server_public_key = "{REAL_KEY}"\n'
        "lan_endpoints = [\n"
        '    { network = "192.0.2.0/24", address = "192.0.2.141", port = 51931 },\n'
        "]\n"
    )
    code, output, rendered = render(renderer, repo, LIVE, tmp_path)
    assert code == 0, output
    assert "lan_endpoints" not in rendered
    assert "192.0.2." not in rendered


def test_a_real_lan_endpoints_block_on_the_router_is_preserved(renderer, tmp_path):
    """Removal is the fallback, not the policy. A router that HAS the block keeps
    it, or every deploy would silently undo the operator's configuration."""
    real_block = (
        "lan_endpoints = [\n"
        '    { network = "10.99.30.0/24", address = "10.99.30.11", port = 51931 },\n'
        "]\n"
    )
    repo = (
        '[home]\nendpoint = "dns-e.realhome.net"\n'
        f'server_public_key = "{REAL_KEY}"\n'
        "lan_endpoints = [\n"
        '    { network = "192.0.2.0/24", address = "192.0.2.141", port = 51931 },\n'
        "]\n"
    )
    code, output, rendered = render(renderer, repo, LIVE + real_block, tmp_path)
    assert code == 0, output
    assert "10.99.30.11" in rendered
    assert "192.0.2." not in rendered


@pytest.mark.parametrize(
    "value",
    [
        "host.invalid",          # RFC 2606
        "thing.example",         # RFC 6761
        "box.test",              # RFC 6761
        "example.com",           # RFC 2606
        "198.51.100.7",          # RFC 5737
        "203.0.113.33",          # RFC 5737
        "2001:db8::1",           # RFC 3849
    ],
)
def test_any_reserved_value_that_survives_stops_the_deploy(renderer, tmp_path, value):
    """THE LAST WORD, AND IT KNOWS NO FIELD NAMES.

    A closed list of names is a list somebody will forget to extend - which is
    exactly how `endpoint` was missed when `server_public_key` was fixed. This
    walks the PARSED config, so the next scrubbed field is a failed deploy on a
    runner rather than a router in a hotel with no uplink.
    """
    repo = (
        '[home]\nendpoint = "dns-e.realhome.net"\n'
        f'server_public_key = "{REAL_KEY}"\n'
        f'\n[agent]\nsomething_new = "{value}"\n'
    )
    code, output, _ = render(renderer, repo, LIVE, tmp_path)
    assert code != 0, f"{value} was allowed through"
    assert "reserved for documentation" in output
    assert "NOTHING was sent" in output


def test_reserved_values_in_COMMENTS_are_not_findings(renderer, tmp_path):
    """The guard reads values, not text, and it has to.

    `zippie.toml` explains the home-over-the-wire behaviour using 203.0.113.33
    in prose. A textual check would refuse the repo's own documentation and the
    fix would be to delete the explanation, which is the wrong direction.
    """
    repo = (
        "# the house's public address is 203.0.113.33 and 192.0.2.0/24 is the LAN\n"
        "# see also pathbond.ts.example-home.invalid\n"
        '[home]\nendpoint = "dns-e.realhome.net"\n'
        f'server_public_key = "{REAL_KEY}"\n'
    )
    code, output, rendered = render(renderer, repo, LIVE, tmp_path)
    assert code == 0, output
    assert "203.0.113.33" in rendered, "the comment was stripped"


def test_the_real_repo_config_renders_against_a_realistic_router(
    renderer, tmp_path
):
    """Against the file that actually ships, not only fixtures.

    This is the test that would have gone red before the endpoint scrub was
    found: the repo's own config, rendered against a router holding real values,
    must come out with nothing reserved in it.
    """
    live = (
        '[home]\nendpoint = "dns-e.realhome.net"\n'
        f'server_public_key = "{REAL_KEY}"\n'
    )
    code, output, rendered = render(
        renderer, LIVE_CONFIG_IN_REPO.read_text(), live, tmp_path
    )
    assert code == 0, output
    values = [
        line for line in rendered.splitlines()
        if not line.lstrip().startswith("#") and re.search(r"=\s*\S", line)
    ]
    for line in values:
        assert ".invalid" not in line, f"a reserved value ships in: {line.strip()}"
        assert "192.0.2." not in line, f"a reserved value ships in: {line.strip()}"


def test_the_repo_config_is_still_valid_toml_after_rendering(renderer, tmp_path):
    """Removing a block by regex is exactly the sort of edit that can leave a
    dangling bracket, and the agent needs this file to start - so a malformed
    render is a router with no agent."""
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib
    code, output, rendered = render(
        renderer, LIVE_CONFIG_IN_REPO.read_text(),
        '[home]\nendpoint = "dns-e.realhome.net"\n'
        f'server_public_key = "{REAL_KEY}"\n',
        tmp_path,
    )
    assert code == 0, output
    tomllib.loads(rendered)


def test_the_live_config_is_read_before_anything_is_sent(deploy_text=None):
    """The renderer needs the router's values, so the deploy has to fetch them -
    and it must do so before the arming block, since a render that fails must
    leave the router completely untouched."""
    text = DEPLOY.read_text()
    fetch = text.index('> "${LIVE_CONFIG}"')
    arm = text.index('say "arming the rollback"')
    copy = text.index('say "copying package"')
    assert fetch < arm < copy
