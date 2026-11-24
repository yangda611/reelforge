import json

from novelvideo.chat.turn_images import referenced_image_paths


def test_canvas_visual_overview_is_real_image_input_and_stays_project_scoped(tmp_path):
    uploads = tmp_path / 'freezone' / '_uploads'
    uploads.mkdir(parents=True)
    image = uploads / 'overview.jpg'
    image.write_bytes(b'image-compaction-validates-content-later')
    attachments = [
        {'type': 'canvas_visual_overview', 'content': json.dumps({
            'source_url': '/static/projects/p/freezone/_uploads/overview.jpg',
        })},
        {'type': 'canvas_visual_overview', 'content': json.dumps({
            'source_url': 'https://untrusted.test/image.jpg',
        })},
        {'type': 'canvas_visual_overview', 'content': '{broken'},
    ]

    assert referenced_image_paths(attachments, tmp_path) == [image]
