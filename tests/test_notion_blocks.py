from worker.notion_blocks import MAX_BLOCKS, build_children

META = {"title": "Neural networks", "channel": "3Blue1Brown", "duration_sec": 1120}
URL = "https://www.youtube.com/watch?v=aircAruvnKk"


def synthesis(n_sections=5, n_bullets=4):
    return {
        "overview": "An overview.",
        "takeaways": ["t1", "t2", "t3"],
        "sections": [
            {"heading": f"Section {i}", "start_sec": i * 200, "bullets": [f"b{i}.{j}" for j in range(n_bullets)]}
            for i in range(n_sections)
        ],
    }


def frames(timestamps):
    return [{"timestamp_sec": t, "caption": f"cap {t}", "reason": f"why {t}", "upload_id": f"u{t}"} for t in timestamps]


def images(blocks):
    return [b for b in blocks if b["type"] == "image"]


def test_every_frame_has_caption_and_reason():
    blocks = build_children(meta=META, synthesis=synthesis(), frames=frames([30, 250, 900]), video_url=URL, frames_requested=3)
    imgs = images(blocks)
    assert len(imgs) == 3
    for img in imgs:
        text = "".join(t["text"]["content"] for t in img["image"]["caption"])
        assert "cap" in text and "why" in text
        assert img["image"]["type"] == "file_upload"


def test_frames_interleaved_under_matching_section():
    blocks = build_children(meta=META, synthesis=synthesis(), frames=frames([250]), video_url=URL, frames_requested=1)
    idx_img = next(i for i, b in enumerate(blocks) if b["type"] == "image")
    prev_heading = next(b for b in reversed(blocks[:idx_img]) if b["type"] == "heading_3")
    assert "Section 1" in "".join(t["text"]["content"] for t in prev_heading["heading_3"]["rich_text"])


def test_missing_frames_produce_notice_but_no_substitute_image():
    blocks = build_children(meta=META, synthesis=synthesis(), frames=frames([30]), video_url=URL, frames_requested=3)
    assert len(images(blocks)) == 1
    assert any(b["type"] == "callout" and "could not be captured" in b["callout"]["rich_text"][0]["text"]["content"] for b in blocks)


def test_text_only_page_when_all_frames_fail():
    blocks = build_children(meta=META, synthesis=synthesis(), frames=[], video_url=URL, frames_requested=4)
    assert images(blocks) == []
    assert any(b["type"] == "bulleted_list_item" for b in blocks)


def test_block_limit_respected_and_frames_kept():
    blocks = build_children(meta=META, synthesis=synthesis(n_sections=12, n_bullets=10),
                            frames=frames([i * 100 for i in range(8)]), video_url=URL, frames_requested=8)
    assert len(blocks) <= MAX_BLOCKS
    assert len(images(blocks)) == 8
