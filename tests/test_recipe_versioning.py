"""Recipe versioning (plan Phase 4 exit criterion).

A run records the recipe version it compiled at; the registry must be able to
reproduce that compile long after the recipe evolves. These tests pin the
registry invariants and the pinned-compile contract, so bumping a version
without registering the old compiler fails CI instead of orphaning runs.
"""

import pytest

from app.services.harness.compiler import (
    _COMPILER_VERSIONS,
    _COMPILERS,
    RECIPES,
    CompileError,
    compile_recipe,
)

CAPS = {
    "capabilities": {
        "segmentation": {
            "key": "segmentation",
            "available": True,
            "limits": {"maxClipSeconds": 120},
            "detail": {"autoMatte": True},
        },
    }
}
PARAMS = {"range": {"start": 1.0, "end": 5.0}, "text": "HELLO"}


def test_every_recipe_version_ever_shipped_keeps_its_compiler():
    for recipe_id, recipe in RECIPES.items():
        versions = _COMPILER_VERSIONS.get(recipe_id, {})
        current = int(recipe["version"])
        assert current in versions, f"{recipe_id} v{current} is not registered"
        # The registry's newest entry IS the current one — a bump without a
        # matching registration (or vice versa) is the bug this catches.
        assert max(versions) == current
        assert versions[current] is _COMPILERS[recipe_id]


def test_unpinned_and_current_pinned_compiles_are_identical():
    unpinned = compile_recipe(
        "subject_behind_text", PARAMS, capability_snapshot=CAPS, video_duration=30.0
    )
    pinned = compile_recipe(
        "subject_behind_text",
        PARAMS,
        capability_snapshot=CAPS,
        video_duration=30.0,
        version=int(RECIPES["subject_behind_text"]["version"]),
    )
    assert unpinned.model_dump() == pinned.model_dump()
    assert unpinned.recipeVersion == RECIPES["subject_behind_text"]["version"]


def test_a_version_that_never_shipped_fails_loudly():
    with pytest.raises(CompileError) as exc:
        compile_recipe(
            "subject_behind_text",
            PARAMS,
            capability_snapshot=CAPS,
            video_duration=30.0,
            version=99,
        )
    assert exc.value.code == "version_unavailable"


def test_unknown_recipes_still_fail_with_their_own_code():
    with pytest.raises(CompileError) as exc:
        compile_recipe("no_such_recipe", {}, capability_snapshot=CAPS, video_duration=30.0)
    assert exc.value.code == "unknown_recipe"
