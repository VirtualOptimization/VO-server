from server.services.transform.roomplan import _extract_usage_front_2d


def test_usage_front_is_the_back_vector_axis():
    # The app's frontVector is local -Z; pieces are used from their +Z side.
    obj = {"frontVector": [0.0, 0.0, -1.0], "backVector": [0.0, 0.0, 1.0]}
    assert _extract_usage_front_2d(obj, 0.0, [1.0, 0.0]) == [0.0, 1.0]


def test_usage_front_without_back_vector_flips_front_vector():
    obj = {"frontVector": [1.0, 0.0, 0.0]}
    assert _extract_usage_front_2d(obj, 0.0, [0.0, 1.0]) == [-1.0, 0.0]
