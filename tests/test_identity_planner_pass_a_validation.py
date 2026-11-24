from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelResponse, RetryPromptPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from novelvideo.agents.identity_planner import (
    DefaultIdentityRequirement,
    EpisodeDefaultIdentities,
    IdentityPlanner,
)
from novelvideo.models import CharacterIdentity, NovelCharacter


class _CastStore:
    state_dir = None

    def __init__(self, characters):
        self._characters = {c.name: c for c in characters}
        self._aliases = {}
        for char in characters:
            for alias in getattr(char, "aliases", []) or []:
                self._aliases[alias] = char.name

    def resolve_name(self, name):
        return self._aliases.get(name, name)

    def get_character(self, name):
        return self._characters.get(self.resolve_name(name))

    def get_all_characters(self):
        return list(self._characters.values())


def _sequenced_model(payloads, captured_messages):
    call_index = 0

    def model_function(messages, agent_info):
        nonlocal call_index
        captured_messages.append(messages)
        payload = payloads[min(call_index, len(payloads) - 1)]
        call_index += 1
        output_tool = agent_info.output_tools[0]
        return ModelResponse(parts=[ToolCallPart(output_tool.name, payload)])

    return FunctionModel(model_function)


def _retry_parts(messages):
    return [
        part
        for message in messages
        for part in getattr(message, "parts", [])
        if isinstance(part, RetryPromptPart)
    ]


def test_episode_default_identities_schema_requires_defaults():
    schema = EpisodeDefaultIdentities.model_json_schema()
    assert "defaults" in schema.get("required", [])


def test_validate_default_requirements_resolves_trimmed_alias_and_prefixed_name():
    store = _CastStore(
        [
            NovelCharacter(
                name="艾莉森",
                aliases=["Alison"],
                identities=[
                    CharacterIdentity(
                        identity_id="艾莉森_律师时期",
                        character_name="艾莉森",
                        identity_name="律师时期",
                    )
                ],
            ),
            NovelCharacter(name="乔瑟夫"),
        ]
    )
    planner = IdentityPlanner(store)

    validated = planner._validate_default_requirements(
        [
            DefaultIdentityRequirement(
                character_name=" alison ",
                visual_state="律师时期",
                reason="复用别名匹配",
            ),
            DefaultIdentityRequirement(
                character_name="乔瑟夫_中年时期",
                visual_state="中年时期",
                reason="去掉角色名前缀",
            ),
        ],
        ["艾莉森", "乔瑟夫"],
    )

    assert [(item.character_name, item.visual_state) for item in validated] == [
        ("艾莉森", "律师时期"),
        ("乔瑟夫", "中年时期"),
    ]


@pytest.mark.asyncio
async def test_analyze_default_identities_retries_on_empty_or_incomplete_output(
    monkeypatch,
):
    store = _CastStore(
        [
            NovelCharacter(name="乔瑟夫"),
            NovelCharacter(name="艾莉森"),
            NovelCharacter(name="达里尔"),
            NovelCharacter(name="艾莉森的弟弟"),
        ]
    )
    empty_payload = {"defaults": []}
    wrong_name_payload = {
        "defaults": [
            {
                "character_name": "乔瑟夫",
                "visual_state": "中年时期",
                "age_group": "middle",
                "reason": "主线形态",
            },
            {
                "character_name": "杰",
                "visual_state": "青年时期",
                "age_group": "youth",
                "reason": "用了别名外的名字",
            },
        ]
    }
    valid_payload = {
        "defaults": [
            {
                "character_name": "乔瑟夫",
                "visual_state": "中年时期",
                "age_group": "middle",
                "reason": "主线形态",
            },
            {
                "character_name": "艾莉森",
                "visual_state": "律师时期",
                "age_group": "youth",
                "reason": "主线形态",
            },
            {
                "character_name": "达里尔",
                "visual_state": "青年时期",
                "age_group": "youth",
                "reason": "主线形态",
            },
            {
                "character_name": "艾莉森的弟弟",
                "visual_state": "青年时期",
                "age_group": "youth",
                "reason": "回忆中的青年形态",
            },
        ]
    }

    captured_messages = []
    model = _sequenced_model(
        [empty_payload, wrong_name_payload, valid_payload],
        captured_messages,
    )
    monkeypatch.setattr(
        IdentityPlanner,
        "_identity_model",
        staticmethod(lambda _env, **_kwargs: model),
    )

    planner = IdentityPlanner(store)
    episode = SimpleNamespace(number=2, title="第2集")
    result = await planner._analyze_default_identities(
        episode,
        cast_names=["乔瑟夫", "艾莉森", "达里尔", "艾莉森的弟弟"],
        content_text="第2集回忆片段：艾莉森与乔瑟夫、达里尔和艾莉森的弟弟对话。",
    )

    assert [d.character_name for d in result.defaults] == [
        "乔瑟夫",
        "艾莉森",
        "达里尔",
        "艾莉森的弟弟",
    ]
    assert len(captured_messages) == 3
    assert not _retry_parts(captured_messages[0])
    retry_1 = _retry_parts(captured_messages[1])
    retry_2 = _retry_parts(captured_messages[2])
    assert retry_1 and "Pass A 默认身份覆盖不完整" in str(retry_1[-1].content)
    assert retry_2 and "杰" in str(retry_2[-1].content)


@pytest.mark.asyncio
async def test_analyze_default_identities_surfaces_missing_chars_and_logs_raw_output(
    monkeypatch,
):
    store = _CastStore(
        [
            NovelCharacter(name="乔瑟夫"),
            NovelCharacter(name="艾莉森"),
        ]
    )
    captured_messages = []
    model = _sequenced_model([{"defaults": []}], captured_messages)
    monkeypatch.setattr(
        IdentityPlanner,
        "_identity_model",
        staticmethod(lambda _env, **_kwargs: model),
    )

    logs = []
    planner = IdentityPlanner(store)
    episode = SimpleNamespace(number=2, title="第2集")

    with pytest.raises(
        RuntimeError,
        match=r"Pass A 默认身份分析失败: Pass A 默认身份覆盖不完整: 乔瑟夫, 艾莉森",
    ):
        await planner._analyze_default_identities(
            episode,
            on_log=logs.append,
            cast_names=["乔瑟夫", "艾莉森"],
            content_text="第2集：乔瑟夫与艾莉森在法庭外相遇。",
        )

    assert len(captured_messages) == 3
    assert any("Pass A 原始输出(0)" in entry for entry in logs)
    assert any("Pass A 默认身份覆盖不完整: 乔瑟夫, 艾莉森" in entry for entry in logs)


def test_is_relational_descriptor_matches_kinship_phrases_only():
    from novelvideo.structured_extraction import is_relational_descriptor

    assert is_relational_descriptor("艾莉森的弟弟") is True
    assert is_relational_descriptor("Alison's brother") is True
    assert is_relational_descriptor("杰") is False
    assert is_relational_descriptor("弟弟") is False


@pytest.mark.asyncio
async def test_adjudication_folds_relational_descriptor_into_named_character():
    from novelvideo.structured_extraction import MergedCharacter, adjudicate_characters

    class _RelationalAdjudicator:
        async def run(self, _prompt):
            from novelvideo.structured_extraction import (
                CharacterAdjudication,
                SamePersonGroup,
            )

            return SimpleNamespace(
                output=CharacterAdjudication(
                    groups=[
                        SamePersonGroup(
                            canonical_name="艾莉森的弟弟",
                            alias_names=["杰"],
                            reason="艾莉森的弟弟就是杰",
                        )
                    ]
                )
            )

    named = MergedCharacter(
        name="杰",
        evidence=[{"source_start": 0, "source_end": 1, "evidence_text": "杰"}] * 3,
    )
    relational = MergedCharacter(
        name="艾莉森的弟弟",
        evidence=[
            {"source_start": 0, "source_end": 6, "evidence_text": "艾莉森的弟弟"}
        ]
        * 5,
    )

    result = await adjudicate_characters(
        [named, relational],
        agent=_RelationalAdjudicator(),
    )

    assert [item.name for item in result] == ["杰"]
    assert result[0].aliases == {"艾莉森的弟弟"}


@pytest.mark.asyncio
async def test_sqlite_merge_character_preserves_target_default_and_aliases(tmp_path):
    from novelvideo.models import NovelEpisode
    from novelvideo.sqlite_store import SQLiteStore

    project_dir = tmp_path / "user" / "project"
    project_dir.mkdir(parents=True)
    store = SQLiteStore(
        "user/project", output_dir=str(project_dir), state_dir=str(project_dir)
    )
    await store.initialize()
    try:
        await store.add_character(
            NovelCharacter(
                name="杰",
                gender="男",
                identities=[
                    CharacterIdentity(
                        identity_id="杰_青年时期",
                        character_name="杰",
                        identity_name="青年时期",
                    )
                ],
            )
        )
        await store.add_character(
            NovelCharacter(
                name="艾莉森的弟弟",
                description="艾莉森的弟弟，在第二集出现",
            )
        )
        await store.add_episode(
            NovelEpisode(
                number=1,
                title="ep1",
                character_names=["杰", "艾莉森的弟弟"],
                identity_ids=["杰_青年时期"],
                identity_default_map={"杰": "杰_青年时期", "艾莉森的弟弟": "艾莉森的弟弟_旧"},
            )
        )

        await store.merge_character("艾莉森的弟弟", "杰")

        assert store.get_character("艾莉森的弟弟") is store.get_character("杰")
        merged = store.get_character("杰")
        assert merged is not None
        assert "艾莉森的弟弟" in merged.aliases
        assert merged.description == "艾莉森的弟弟，在第二集出现"
        ep1 = store.get_episode(1)
        assert ep1 is not None
        assert ep1.character_names == ["杰"]
        assert ep1.identity_default_map == {"杰": "杰_青年时期"}
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_reconcile_relational_characters_merges_only_verified_matches(
    tmp_path, monkeypatch
):
    from novelvideo.models import NovelEpisode
    from novelvideo.sqlite_store import SQLiteStore

    project_dir = tmp_path / "user" / "project"
    project_dir.mkdir(parents=True)
    store = SQLiteStore(
        "user/project", output_dir=str(project_dir), state_dir=str(project_dir)
    )
    await store.initialize()
    try:
        await store.add_character(NovelCharacter(name="杰", gender="男"))
        await store.add_character(NovelCharacter(name="艾莉森", gender="女"))
        await store.add_character(NovelCharacter(name="艾莉森的弟弟", gender="男"))
        await store.add_character(NovelCharacter(name="艾莉森的姐姐", gender="女"))
        await store.add_character(NovelCharacter(name="达里尔", gender="男"))
        await store.add_episode(
            NovelEpisode(
                number=1,
                title="ep1",
                raw_content="艾莉森看着窗外，回忆起艾莉森的弟弟杰小时候的样子。",
            )
        )
        await store.add_episode(
            NovelEpisode(
                number=2,
                title="ep2",
                raw_content="艾莉森的弟弟敲门进来，达里尔抬头看向艾莉森的姐姐。",
            )
        )

        captured_messages = []
        payload = {
            "matches": [
                {
                    "relational_name": "艾莉森的弟弟",
                    "canonical_name": "杰",
                    "confirmed": True,
                    "evidence_quote": "艾莉森的弟弟杰",
                    "reason": "第1集明确点明艾莉森的弟弟就是杰",
                },
                {
                    "relational_name": "艾莉森的姐姐",
                    "canonical_name": "达里尔",
                    "confirmed": True,
                    "evidence_quote": "达里尔抬头看向艾莉森的姐姐",
                    "reason": "故意制造性别冲突的错误匹配",
                },
            ]
        }
        model = _sequenced_model([payload], captured_messages)
        monkeypatch.setattr(
            IdentityPlanner,
            "_identity_model",
            staticmethod(lambda _env, **_kwargs: model),
        )

        logs = []
        planner = IdentityPlanner(store)
        await planner._reconcile_relational_characters(
            store.get_episode(2),
            "艾莉森的弟弟敲门进来，达里尔抬头看向艾莉森的姐姐。",
            on_log=logs.append,
        )

        assert store.resolve_name("艾莉森的弟弟") == "杰"
        assert "艾莉森的弟弟" in store.get_character("杰").aliases
        # Gender conflict prevents bad merge for 艾莉森的姐姐 -> 达里尔
        assert store.get_character("艾莉森的姐姐").name == "艾莉森的姐姐"
        assert any("自动合并同指角色: 艾莉森的弟弟 -> 杰" in line for line in logs)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_reconcile_relational_characters_is_noop_when_no_relational_names(
    tmp_path, monkeypatch
):
    from novelvideo.models import NovelEpisode
    from novelvideo.sqlite_store import SQLiteStore

    project_dir = tmp_path / "user" / "project"
    project_dir.mkdir(parents=True)
    store = SQLiteStore(
        "user/project", output_dir=str(project_dir), state_dir=str(project_dir)
    )
    await store.initialize()
    try:
        await store.add_character(NovelCharacter(name="杰", gender="男"))
        await store.add_character(NovelCharacter(name="艾莉森", gender="女"))
        called = False

        def fail_if_called(*_args, **_kwargs):
            nonlocal called
            called = True
            raise AssertionError("should not construct model")

        monkeypatch.setattr(IdentityPlanner, "_identity_model", staticmethod(fail_if_called))
        planner = IdentityPlanner(store)
        await planner._reconcile_relational_characters(
            NovelEpisode(number=2, title="ep2"),
            "杰和艾莉森对话。",
        )
        assert called is False
    finally:
        await store.close()
