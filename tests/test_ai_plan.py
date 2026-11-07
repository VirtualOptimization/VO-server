from workers.optimizer import ai_constraints as ai


def _problem():
    return {"room_metadata": {"dimensions": {"width": 4, "depth": 3}}, "fixed_elements": [], "movable_items": [
        {"id": "bed", "type": "bed", "extent": [1, 2, 0.5], "pos": [1, 1, 0.5]},
        {"id": "n", "type": "table", "extent": [0.5, 0.5, 0.5], "pos": [3, 1, 0.5]},
        {"id": "hung", "type": "shelf", "extent": [1, 0.3, 0.5], "pos": [2, 0.2, 2], "pinned": True},
    ]}


def test_plan_keeps_only_valid_relations():
    plan = ai._validate_plan({"furniture_roles": [{"object_id": "n", "role": "nightstand"}, {"object_id": "x", "role": "storage"}],
                              "relations": [{"object_id": "n", "near_object_id": "bed", "zone": "bed_head"},
                                            {"object_id": "n", "near_object_id": "n"},
                                            {"object_id": "n", "near_object_id": "ghost"},
                                            {"object_id": "hung", "near_object_id": "bed"},
                                            {"object_id": "bed", "near_object_id": "n", "zone": "bed_head"}]}, _problem())
    assert [r["object_id"] for r in plan["furniture_roles"]] == ["n"]
    assert [(r["object_id"], r["zone"]) for r in plan["relations"]] == [("n", "bed_head")]


def test_failed_call_leaves_the_problem_usable(monkeypatch):
    monkeypatch.setenv("AI_LAYOUT_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    def boom(*args, **kwargs):
        raise RuntimeError("down")
    monkeypatch.setattr(ai, "_call_claude_once", boom)
    problem = ai.plan_room_intent(_problem())
    assert "ai_plan_relations" not in problem
    assert "down" in problem["ai_plan_error"]


def test_shortened_ids_resolve_only_when_unambiguous():
    ids = {"C58C158A-79B0", "C58C999A-79B0", "FE84C426-8C48"}
    assert ai._resolve_id("FE84C426", ids) == "FE84C426-8C48"
    assert ai._resolve_id("C58C", ids) is None
    assert ai._resolve_id("C58C15", ids) == "C58C158A-79B0"
    assert ai._resolve_id("ZZZZZZ", ids) is None
