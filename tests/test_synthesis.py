from worker.stages.synthesize import chunk_lines, clean_key_frames, merge_chunks, render, transcript_lines


def segs(n, step=5.0, text="word " * 20):
    return [{"start": i * step, "duration": step, "text": text.strip()} for i in range(n)]


def test_transcript_lines_group_and_keep_order():
    lines = transcript_lines(segs(12))
    starts = [s for s, _ in lines]
    assert starts == sorted(starts)
    assert len(lines) == 4  # 12 segments x 5s grouped into ~15s lines
    assert render(lines).startswith("[0:00] ")


def test_chunking_preserves_order_and_content():
    lines = transcript_lines(segs(300))
    chunks = chunk_lines(lines, max_tokens=500)
    assert len(chunks) > 1
    flat = [line for c in chunks for line in c]
    assert flat == lines


def test_single_chunk_when_small():
    lines = transcript_lines(segs(10))
    assert len(chunk_lines(lines, max_tokens=60000)) == 1


def test_merge_keeps_part_order():
    parts = [
        {
            "sections": [{"heading": "B", "start_sec": 50}, {"heading": "A", "start_sec": 10}],
            "key_frames": [{"timestamp_sec": 20}],
        },
        {"sections": [{"heading": "C", "start_sec": 300}], "key_frames": [{"timestamp_sec": 320}]},
    ]
    merged = merge_chunks(parts)
    assert [s["heading"] for s in merged["sections"]] == ["A", "B", "C"]
    assert [f["timestamp_sec"] for f in merged["key_frames"]] == [20, 320]


def test_clean_key_frames_clamps_dedupes_caps():
    frames = [
        {"timestamp_sec": -5, "caption": "start", "reason": "r"},
        {"timestamp_sec": 2, "caption": "too close", "reason": "r"},
        {"timestamp_sec": 100, "caption": "", "reason": "missing caption dropped"},
        {"timestamp_sec": 5000, "caption": "past end", "reason": "r"},
    ] + [{"timestamp_sec": 100 + i * 30, "caption": f"c{i}", "reason": "r"} for i in range(20)]
    out = clean_key_frames(frames, duration=1000, max_frames=8)
    assert len(out) == 8
    ts = [f["timestamp_sec"] for f in out]
    assert ts == sorted(ts)
    assert all(0 <= t <= 999 for t in ts)
    assert all(f["caption"] and f["reason"] for f in out)
