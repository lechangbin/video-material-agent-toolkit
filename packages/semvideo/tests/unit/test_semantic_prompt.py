from pathlib import Path

from semvideo.modules.semantics.prompt import PROMPT_VERSION, segmentation_messages


def test_prompt_balances_semantic_stages_against_shot_changes(tmp_path: Path) -> None:
    assert PROMPT_VERSION == "semantic-grid-v3"
    messages = segmentation_messages(
        anchors=[
            {
                "anchor_id": "anchor_0000",
                "timestamp_ms": 0,
                "reasons": ["start"],
            },
            {
                "anchor_id": "anchor_0001",
                "timestamp_ms": 10_000,
                "reasons": ["semantic"],
            },
        ],
        grids=[],
        transcript_spans=[],
        job_root=tmp_path,
    )

    instructions = messages[1]["content"][0]["text"]
    assert "拍摄角度变化本身都不是语义边界" in instructions
    assert "同一总任务可以包含多个可独立理解、检索和复用的叙事阶段" in instructions
    assert "不要仅因整条视频主题统一就合并成一个片段" in instructions
    assert "不预设片段数量" in instructions
    assert "允许使用最接近真实语义转折的锚点" in instructions
    assert "前一片段的 end_anchor_id 必须等于后一片段的 start_anchor_id" in instructions
