from semvideo.modules.media.subtitles import parse_srt


def test_parse_srt_handles_tags_and_multiline_text() -> None:
    spans = parse_srt(
        """1
00:00:00,900 --> 00:00:02,600
<i>用户打开视频</i>
导入窗口。
"""
    )

    assert len(spans) == 1
    assert spans[0].start_ms == 900
    assert spans[0].end_ms == 2600
    assert spans[0].text == "用户打开视频 导入窗口。"
