"""The router chaos harness must verify that its blackhole catches real traffic."""

from pathlib import Path
import subprocess


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "chaos.sh"


def test_wifi_blackhole_requires_a_packet_filter_and_counts_dropped_probes():
    shell = f'''
CHAOS_LIB_ONLY=1 . "{SCRIPT}"
LOG=/dev/null
counter_file=$(mktemp)
iptables() {{
    case "$*" in
        *"-C "*) return 1 ;;
        *"-I OUTPUT"*) output_rule=1 ;;
        *"-L OUTPUT"*)
            if [ "$output_rule" = 1 ]; then
                count=$(cat "$counter_file")
                echo "$count 504 DROP all -- * apclix0 0.0.0.0/0 0.0.0.0/0 /* zippie-chaos-apclix0 */"
            fi ;;
    esac
    return 0
}}
probe_loss() {{ echo 6 > "$counter_file"; echo 100; }}
echo 0 > "$counter_file"
blackhole_on apclix0 || exit 1
[ "$output_rule" = 1 ] || exit 2
verify_impairment apclix0 blackhole '' || exit 3
rm "$counter_file"
'''
    result = subprocess.run(["sh", "-c", shell], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_wifi_blackhole_rejects_loss_when_no_packets_hit_the_filter():
    shell = f'''
CHAOS_LIB_ONLY=1 . "{SCRIPT}"
LOG=/dev/null
iptables() {{
    case "$*" in
        *"-L OUTPUT"*) echo '0 0 DROP all -- * apclix0 0.0.0.0/0 0.0.0.0/0 /* zippie-chaos-apclix0 */' ;;
    esac
    return 0
}}
probe_loss() {{ echo 100; }}
verify_impairment apclix0 blackhole ''
'''
    result = subprocess.run(["sh", "-c", shell], capture_output=True, text=True, check=False)
    assert result.returncode != 0
