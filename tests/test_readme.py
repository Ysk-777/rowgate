"""The README shows what RowGate really does, and the other documents hold together.

Every command in the README with an output block after it is marked
``<!-- readme-test: run in examples|empty, exit N -->``. This test runs each
one, in README order, in a copy of examples/ or in an empty folder, and
checks that the exit code and the output (stdout, then stderr) match the
README exactly. Every ``text`` and ``json`` block in the README must be such
an output, so none can be written by hand.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from rowgate.loader import load_rules

ROOT = Path(__file__).parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
EXAMPLE = re.compile(
    r"<!-- readme-test: run in (?P<where>examples|empty), exit (?P<code>[0-9]) -->\n"
    r"```bash\n(?P<command>rowgate [^\n]*)\n```\n"
    r"(?:(?!```).)*?"  # text between the command and its output, such as <details>
    r"```(?:text|json)\n(?P<output>.*?)\n```",
    re.DOTALL,
)
EXAMPLES = list(EXAMPLE.finditer(README))


def run_example(command: str, folder: Path) -> tuple[int, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("FORCE_COLOR", "NO_COLOR")}
    completed = subprocess.run(
        [sys.executable, "-m", "rowgate", *shlex.split(command)[1:]],
        cwd=folder,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )
    return completed.returncode, (completed.stdout + completed.stderr).rstrip("\n")


def test_every_readme_example_gives_the_output_shown(tmp_path: Path) -> None:
    shutil.copytree(ROOT / "examples", tmp_path / "examples")
    (tmp_path / "empty").mkdir()
    assert len(EXAMPLES) >= 10
    for example in EXAMPLES:  # in order: rowgate init comes before validate
        code, output = run_example(example["command"], tmp_path / example["where"])
        assert (example["command"], code) == (example["command"], int(example["code"]))
        assert output == example["output"], example["command"]


def test_every_output_block_in_the_readme_comes_from_a_command() -> None:
    blocks = re.findall(r"^```(?:text|json)$", README, re.MULTILINE)
    assert len(blocks) == len(EXAMPLES)


def test_the_readme_examples_cover_text_json_errors_and_every_exit_code() -> None:
    commands = [example["command"] for example in EXAMPLES]
    assert {int(example["code"]) for example in EXAMPLES} == {0, 1, 2}
    assert any("--format json" in command for command in commands)
    assert any("--no-samples" in command for command in commands)
    assert "rowgate init" in commands


def yaml_blocks() -> list[str]:
    return re.findall(r"^```yaml\n(.*?)^```$", README, re.MULTILINE | re.DOTALL)


@pytest.mark.parametrize(
    "block", [block for block in yaml_blocks() if block.startswith("version: 1")]
)
def test_every_rules_file_in_the_readme_is_valid(tmp_path: Path, block: str) -> None:
    rules = tmp_path / "rules.yaml"
    rules.write_text(block, encoding="utf-8")
    assert load_rules(rules).version == 1


def test_the_readme_shows_the_real_shipments_rules() -> None:
    real = (ROOT / "examples" / "shipments.yaml").read_text(encoding="utf-8")
    without_comments = "".join(line for line in real.splitlines(True) if not line.startswith("#"))
    assert without_comments in yaml_blocks()


def test_the_roadmap_total_is_the_sum_of_its_items() -> None:
    roadmap = (ROOT / "ROADMAP.md").read_text(encoding="utf-8")
    hours = [int(h) for h in re.findall(r"^Estimate: ([0-9]+) hours\.$", roadmap, re.MULTILINE)]
    items = re.findall(r"^### ([0-9]+)\. ", roadmap, re.MULTILINE)
    assert items == [str(number) for number in range(1, len(items) + 1)]
    assert len(hours) == len(items)
    assert f"**Total: {sum(hours)} hours** for the {len(items)} items below." in roadmap


DOCUMENTS = ["README.md", "CONTRIBUTING.md", "ROADMAP.md", "CHANGELOG.md", "examples/README.md"]


@pytest.mark.parametrize("name", DOCUMENTS)
def test_links_between_documents_resolve(name: str) -> None:
    path = ROOT / name
    text = path.read_text(encoding="utf-8")
    for target in re.findall(r"\]\(([^)]+)\)", text):
        if target.startswith(("http://", "https://")):
            continue
        file_part, _, anchor = target.partition("#")
        linked = (path.parent / file_part) if file_part else path
        assert linked.exists(), f"{name}: {target}"
        if anchor:
            headings = re.findall(r"^#+ (.*)$", linked.read_text(encoding="utf-8"), re.MULTILINE)
            slugs = {re.sub(r"[^a-z0-9 _-]", "", h.lower()).replace(" ", "-") for h in headings}
            assert anchor in slugs, f"{name}: {target}"
