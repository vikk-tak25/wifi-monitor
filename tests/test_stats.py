import pytest

from wifimon.stats import link_stats, mean_abs_delta, network_stats, percentile, rssi_series, signal_quality


def scans(n):
    return [{"id": i + 1, "ts": 100.0 + i * 15} for i in range(n)]


def obs(scan_id, bssid, rssi, channel=6, ssid="Net"):
    return {"scan_id": scan_id, "ts": 100.0 + (scan_id - 1) * 15, "bssid": bssid, "ssid": ssid,
            "channel": channel, "rssi": rssi, "auth": 3, "hidden": 0}


def test_percentile_and_jitter():
    assert percentile([], 95) is None
    assert percentile([5], 95) == 5
    assert percentile([1, 2, 3, 4, 5], 50) == 3
    assert percentile([1, 2, 3, 4], 95) == pytest.approx(3.85)
    assert mean_abs_delta([1]) is None
    assert mean_abs_delta([1, 3, 2]) == pytest.approx(1.5)


def test_steady_network_scores_high():
    rows = [obs(i, "aa", -50) for i in range(1, 11)]
    [net] = network_stats(scans(10), rows)
    assert net["seen_pct"] == 100
    assert net["std_rssi"] == 0
    assert net["dropouts"] == 0
    assert net["visible_now"] and net["current_rssi"] == -50
    assert net["stability"] == 100
    assert net["quality"] == "excellent"


def test_flaky_network_counts_dropouts_and_gaps():
    # Seen in scans 1, 2, 5, 6, 10 of 10: two gaps (2 and 3 scans long).
    rows = [obs(i, "bb", r) for i, r in ((1, -70), (2, -78), (5, -66), (6, -80), (10, -72))]
    [net] = network_stats(scans(10), rows)
    assert net["samples"] == 5
    assert net["seen_pct"] == 50
    assert net["dropouts"] == 2
    assert net["longest_gap_scans"] == 3
    assert net["min_rssi"] == -80 and net["max_rssi"] == -66
    assert net["mean_rssi"] == pytest.approx(-73.2)
    assert net["jitter_db"] == pytest.approx((8 + 12 + 14 + 8) / 4)
    assert net["stability"] < 50
    assert net["visible_now"] is True


def test_network_missing_from_latest_scan_has_no_current_rssi():
    rows = [obs(1, "cc", -60), obs(2, "cc", -61)]
    [net] = network_stats(scans(3), rows)
    assert net["visible_now"] is False
    assert net["current_rssi"] is None


def test_networks_sorted_strongest_first_and_channel_changes_counted():
    rows = [obs(1, "weak", -85), obs(1, "strong", -40, channel=1), obs(2, "strong", -42, channel=6)]
    result = network_stats(scans(2), rows)
    assert [n["bssid"] for n in result] == ["strong", "weak"]
    assert result[0]["channel_changes"] == 1
    assert result[0]["channel"] == 6


def test_rssi_series_aligns_to_scans_with_gaps():
    rows = [obs(1, "aa", -50), obs(3, "aa", -52), obs(2, "bb", -70)]
    series = rssi_series(scans(3), rows, ["aa", "zz"])
    assert series["ts"] == [100.0, 115.0, 130.0]
    assert series["series"] == {"aa": [-50, None, -52], "zz": [None, None, None]}


def sample(ts, connected=True, rssi=-55, latency=4.0, ok=True):
    return {"ts": ts, "connected": connected, "rssi": rssi if connected else None,
            "latency_ms": latency if connected and ok else None, "ok": connected and ok}


def test_link_stats_loss_latency_and_outages():
    samples = [sample(0, latency=2), sample(1, latency=4), sample(2, ok=False), sample(3, latency=6),
               sample(4, connected=False), sample(5, connected=False), sample(6, latency=8, rssi=-65)]
    s = link_stats(samples, disconnects=1)
    assert s["samples"] == 7
    assert s["uptime_pct"] == pytest.approx(71.4)
    assert s["pings"] == 5 and s["pings_ok"] == 4
    assert s["loss_pct"] == 20
    assert s["latency_mean"] == 5 and s["latency_median"] == 5
    assert s["latency_min"] == 2 and s["latency_max"] == 8
    assert s["latency_jitter"] == 2
    assert s["longest_outage_s"] == 2
    assert s["disconnects"] == 1
    assert s["connected_now"] is True and s["rssi_current"] == -65
    assert s["duration_s"] == 6


def test_link_stats_ongoing_outage_and_empty():
    s = link_stats([sample(0), sample(10, connected=False), sample(25, connected=False)])
    assert s["longest_outage_s"] == 15
    assert s["connected_now"] is False
    empty = link_stats([])
    assert empty["samples"] == 0 and empty["uptime_pct"] is None and empty["loss_pct"] is None


@pytest.mark.parametrize("rssi,label", [(-45, "excellent"), (-55, "good"), (-65, "fair"),
                                        (-75, "weak"), (-90, "poor"), (None, "none")])
def test_signal_quality(rssi, label):
    assert signal_quality(rssi) == label
