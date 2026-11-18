import pytest
from pydantic import ValidationError

from novelvideo.api.schemas import SingleVideoRequest


def test_single_video_request_allows_nine_item_queue_batch():
    request = SingleVideoRequest(batch_id="video-batch", batch_size=9)

    assert request.batch_size == 9


def test_single_video_request_rejects_more_than_nine_items():
    with pytest.raises(ValidationError):
        SingleVideoRequest(batch_id="video-batch", batch_size=10)

