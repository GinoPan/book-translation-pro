from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from book_translation_pro.artifacts import BTPError, atomic_write_json, atomic_write_text, load_json
from book_translation_pro.cli import main, show_unit
from book_translation_pro.config import load_yaml, resolve_config, resolve_project, write_yaml
from book_translation_pro.glossary import build_ledger, ledger_for_unit, new_entry_problem
from book_translation_pro.qa import run_qa
from book_translation_pro.quality import (
    PROFILES,
    heading_paragraphs,
    ledger_checks,
    name_consistency,
    note_marker_gaps,
    profile_for,
    repeated_text,
    split_fragments,
    term_candidates,
    typography_issues,
)
from book_translation_pro.quality_benchmark import (
    PLANTED,
    changed_number,
    drop_sentence,
    fired,
    half_width_punctuation,
    repeat_join,
    repeat_sentence,
)
from book_translation_pro.state import dispatch_unit, load_state, record_review, record_text_output, save_state
from book_translation_pro.translation_review import (
    RECORD,
    TEMPLATE,
    build_translation_review,
    problems,
    record_translation_review,
    rule_proposals,
    score,
)
from tests.unit.test_state_and_qa import make_project

ZH = PROFILES["zh"]
LONG_ZH = "价值流团队建立绩效指标并开发数据收集流程时，会计人员应当充分参与其中。"
LONG_ZH_2 = "传统的标准成本法鼓励大批量生产，因为它把间接费用分摊到更多的产品上。"


def _unit(unit_id: str, source: str, target: str, observation: dict | None = None):
    return (unit_id, source, target, observation or {})


class TypographyTests(unittest.TestCase):
    def test_each_slip_is_reported_with_an_example(self) -> None:
        issues = typography_issues("精益会计,价值流。\n\n成本，，下降。\n\n会计 人员\n\n他说“好。", ZH)
        self.assertEqual(issues["half_width_punctuation"]["example"], "计,")
        self.assertIn("doubled_punctuation", issues)
        self.assertIn("space_between_characters", issues)
        self.assertEqual(issues["unpaired “”"]["count"], 1)

    def test_table_cells_are_checked_too(self) -> None:
        table = "| 项目 | 说明 |\n|---|---|\n| 成本 | 直接人工,间接费用 |\n"
        self.assertIn("half_width_punctuation", typography_issues(table, ZH))

    def test_invisible_characters_are_reported(self) -> None:
        self.assertEqual(typography_issues("## 8.4　管理会计的重大​​发展\n", ZH)["invisible_characters"]["count"], 2)
        self.assertNotIn("invisible_characters", typography_issues("## 8.4　管理会计的重大发展\n", ZH))

    def test_diagram_labels_and_removed_links_are_not_slips(self) -> None:
        labels = ("<!-- btp:figure-labels exhibit=\"10.3\" -->\n1. 节拍时间 _____ 秒   周期时间 ______ 秒\n"
                  "<!-- /btp:figure-labels -->\n\n")
        link = "参见研究小组：<http://www.example.org>；《系统智能》（2005）。\n"
        self.assertEqual(typography_issues(labels + link, ZH), {})
        # The same slips in prose are still found.
        self.assertIn("space_between_characters", typography_issues("节拍时间 秒   周期时间。\n", ZH))
        self.assertIn("doubled_punctuation", typography_issues("参见研究小组：；《系统智能》。\n", ZH))

    def test_correct_text_and_mixed_scripts_are_left_alone(self) -> None:
        clean = ("图表 4.6 显示了价值流成本。Lean Accounting, 2nd edition 是一本书。\n\n"
                 "他说：“这是“精益”的核心。”（见第 3 章）")
        self.assertEqual(typography_issues(clean, ZH), {})

    def test_profiles_are_chosen_by_language_tag(self) -> None:
        self.assertIs(profile_for("zh-Hans"), ZH)
        self.assertIs(profile_for("ZH_cn"), ZH)
        self.assertIsNone(profile_for("de"))


class RepeatedTextTests(unittest.TestCase):
    def test_a_sentence_translated_twice_in_a_unit_fails(self) -> None:
        source = "First point about value streams and costs.\n\nSecond point about the teams that own them."
        target = f"{LONG_ZH}\n\n{LONG_ZH_2}{LONG_ZH}"
        found = repeated_text([_unit("unit-0001", source, target)])
        self.assertEqual([(f["unit_id"], f["where"]) for f in found], [("unit-0001", "within the unit")])

    def test_a_repeat_the_source_also_has_is_left_alone_even_wrapped(self) -> None:
        # The source prints the citation twice: once on one line, once wrapped across two
        # blocks and without its full stop.  The translation repeats it too, rightly.
        source = ("1. Robert S. Kaplan and Robin Cooper, Cost and Effect (Boston: Harvard Business School Press, 1998).\n\n"
                  "2. Robert S. Kaplan and Robin Cooper, Cost and Effect (Boston: Harvard Business\n\n"
                  "School Press, 1998)")
        target = ("1. Robert S. Kaplan 和 Robin Cooper，《成本与效应》（波士顿：哈佛商学院出版社，1998 年）。\n\n"
                  "2. Robert S. Kaplan 和 Robin Cooper，《成本与效应》（波士顿：哈佛商学院出版社，1998 年）。")
        self.assertEqual(repeated_text([_unit("unit-0001", source, target)]), [])

    def test_a_passage_translated_on_both_sides_of_a_join_fails(self) -> None:
        first = _unit("unit-0001", "Teams set the measures for each cell.", f"{LONG_ZH_2}{LONG_ZH}")
        second = _unit("unit-0002", "Accountants then support the teams.", f"{LONG_ZH}\n\n其余内容。")
        found = repeated_text([first, second])
        self.assertEqual([(f["unit_id"], f["where"]) for f in found],
                         [("unit-0002", "across the join with the previous unit")])

    def test_a_join_repeat_the_source_also_has_is_left_alone(self) -> None:
        sentence = "The value stream team owns the measures and the data collection process."
        first = _unit("unit-0001", f"Intro text here. {sentence}", f"{LONG_ZH_2}{LONG_ZH}")
        second = _unit("unit-0002", f"{sentence} More follows.", f"{LONG_ZH}\n\n其余内容。")
        self.assertEqual(repeated_text([first, second]), [])

    def test_short_repeats_are_ignored(self) -> None:
        target = "见下表。\n\n数据如下。\n\n见下表。"
        self.assertEqual(repeated_text([_unit("unit-0001", "See the table. Data follows.", target)]), [])


def _named_units(english_in: int, translated_in: int, name: str = "Deming", gloss: bool = False):
    units = []
    for n in range(english_in + translated_in):
        source = f"The ideas of {name} shaped the plant. Teams followed {name} closely."
        if n < english_in:
            target = f"{name} 的思想影响了工厂。"
        else:
            target = "戴明（Deming）的思想影响了工厂。" if gloss else "戴明的思想影响了工厂。"
        units.append(_unit(f"unit-{n + 1:04d}", source, target))
    return units


class NameConsistencyTests(unittest.TestCase):
    def test_a_name_kept_in_english_in_some_units_only_is_reported(self) -> None:
        found = name_consistency(_named_units(2, 3))
        self.assertEqual([(f["name"], f["english"], len(f["translated"])) for f in found],
                         [("Deming", ["unit-0001", "unit-0002"], 3)])

    def test_a_gloss_in_brackets_counts_as_translated(self) -> None:
        found = name_consistency(_named_units(2, 3, gloss=True))
        self.assertEqual(len(found[0]["translated"]), 3)

    def test_consistent_names_and_lone_exceptions_are_left_alone(self) -> None:
        self.assertEqual(name_consistency(_named_units(5, 0)), [])
        self.assertEqual(name_consistency(_named_units(0, 5)), [])
        self.assertEqual(name_consistency(_named_units(1, 4)), [])  # one English mention may be a citation

    def test_a_word_the_book_also_writes_in_lower_case_is_not_a_name(self) -> None:
        units = _named_units(2, 3, name="Lean")
        units.append(_unit("unit-0099", "We talk about lean in lower case too.", "精益。"))
        self.assertEqual(name_consistency(units), [])

    def test_a_name_with_a_glossary_or_ledger_entry_is_left_to_that_check(self) -> None:
        covered = [{"source": "Deming", "aliases": []}]
        self.assertEqual(name_consistency(_named_units(2, 3), covered), [])

    def test_placeholder_units_are_left_out(self) -> None:
        units = _named_units(2, 1)
        units += [_unit(f"unit-009{n}", "Index: Deming, 5. Deming, Edwards, 7.", "<!-- 原书索引已略去。 -->") for n in range(3)]
        self.assertEqual(name_consistency(units), [])

    def test_a_name_printed_in_capitals_counts_as_a_mention(self) -> None:
        units = _named_units(2, 1)
        units.append(_unit("unit-0099", "W. EDWARDS DEMING\n\nOn Quality.\n", "W. 爱德华兹·戴明\n\n论质量。\n"))
        [item] = name_consistency(units)
        self.assertEqual((item["name"], len(item["translated"])), ("Deming", 2))
        self.assertIn("unit-0099", item["translated"])

    def test_units_kept_in_the_source_language_by_design_are_left_out(self) -> None:
        units = _named_units(2, 3)
        units[0] = units[0][:3] + ({"source_issues": ["untranslated_by_design: bibliography"]},)
        self.assertEqual(name_consistency(units), [])


class LedgerTests(unittest.TestCase):
    def _project(self, root: Path, observations: dict[str, list]) -> None:
        for unit_id, entries in observations.items():
            atomic_write_json(root / "state" / "observations" / f"{unit_id}.json", {
                "schema_version": 1, "new_entries": entries, "conflicts": [], "source_issues": [], "used_glossary_ids": []})

    def test_renderings_are_collected_in_reading_order_with_conflicts_and_skips(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._project(root, {
                "unit-0001": [{"source": "Deming", "target": "戴明", "category": "person", "source_ref": "unit-0001"}],
                "unit-0002": [{"source": "box score", "target": "绩效记分卡", "category": "technical_term"},
                              {"source": "deming", "target": "德明", "category": "person"},
                              {"source": "no target", "category": "other"}],
            })
            ledger = build_ledger(root, ["unit-0001", "unit-0002"])
            self.assertEqual(ledger["skipped"], 1)
            by_source = {e["source"]: e for e in ledger["entries"]}
            self.assertEqual(by_source["Deming"]["targets"], ["戴明", "德明"])
            self.assertEqual(by_source["Deming"]["units"], ["unit-0001", "unit-0002"])
            self.assertEqual(by_source["box score"]["targets"], ["绩效记分卡"])
            self.assertTrue((root / "terminology" / "ledger.json").is_file())

    def test_a_unit_sees_the_ledger_entries_its_source_mentions_unless_the_glossary_has_them(self) -> None:
        ledger = {"entries": [
            {"source": "box score", "category": "technical_term", "targets": ["绩效记分卡"], "units": ["unit-0002"]},
            {"source": "Deming", "category": "person", "targets": ["戴明"], "units": ["unit-0001"]},
        ]}
        glossary = [{"source": "Deming", "aliases": [], "target": "戴明"}]
        chosen = ledger_for_unit(ledger, "The box scores and Deming's ideas.", glossary)
        self.assertEqual([e["source"] for e in chosen], ["box score"])
        self.assertEqual(ledger_for_unit(ledger, "Nothing relevant.", []), [])

    def test_a_unit_that_departs_from_a_recorded_rendering_is_reported(self) -> None:
        ledger = {"entries": [{"source": "box score", "category": "technical_term",
                               "targets": ["绩效记分卡"], "units": ["unit-0001"]}]}
        units = [_unit("unit-0001", "The box score shows it.", "绩效记分卡显示了这一点。"),
                 _unit("unit-0002", "Use the box score weekly.", "每周使用箱线分数表。")]
        found = ledger_checks(units, ledger, {"entries": []})
        self.assertEqual(found, [{"source": "box score", "target": "绩效记分卡", "units": ["unit-0002"]}])
        approved = {"entries": [{"source": "box score", "aliases": [], "status": "approved", "target": "绩效记分卡"}]}
        self.assertEqual(ledger_checks(units, ledger, approved), [])  # the glossary check owns it

    def test_entry_shape(self) -> None:
        self.assertIsNone(new_entry_problem({"source": "Deming", "target": "戴明", "category": "person"}))
        self.assertIn("target", new_entry_problem({"source": "Deming", "category": "person"}))
        self.assertIn("category", new_entry_problem({"source": "Deming", "target": "戴明", "category": "name"}))
        self.assertIsNotNone(new_entry_problem("Deming -> 戴明"))


class TermCandidateTests(unittest.TestCase):
    def test_recurring_phrases_and_names_across_units(self) -> None:
        sources = {
            "unit-0001": "The box score shows value stream costs. Toyota uses the box score. Ask Deming.",
            "unit-0002": "Each box score is weekly. The value stream costs fall. Deming agreed with Toyota.",
            "unit-0003": "A value stream costs less, as Toyota and Deming said. In the end the box score helps.",
        }
        found = term_candidates(sources, 10)
        phrases = {p["source"]: p for p in found["phrases"]}
        self.assertEqual(phrases["box score"]["count"], 4)
        self.assertIn("value stream costs", phrases)
        self.assertNotIn("value stream", phrases)  # only ever part of the longer phrase
        self.assertNotIn("the box", phrases)  # a stop word at the edge
        self.assertEqual({n["source"] for n in found["names"]}, {"Toyota", "Deming"})


class BenchmarkPlantTests(unittest.TestCase):
    SOURCE = "One sentence here. Another sentence there. A third one follows, with 42 units."
    TARGET = f"{LONG_ZH}{LONG_ZH_2}第三句有 42 个单位，内容很多很多很多。"

    def test_each_rule_check_catches_its_planted_error(self) -> None:
        clean = fired("unit-0001", self.SOURCE, self.TARGET, {}, ZH)
        self.assertNotIn("content.repeated_text", clean)
        for plant, check_id in ((repeat_sentence, "content.repeated_text"),
                                (half_width_punctuation, "style.typography"),
                                (changed_number, "content.numbers")):
            changed = plant(self.TARGET, self.SOURCE, None)
            self.assertIsNotNone(changed, plant.__name__)
            self.assertIn(check_id, fired("unit-0001", self.SOURCE, changed, {}, ZH), plant.__name__)

    def test_a_dropped_sentence_is_left_for_the_reader(self) -> None:
        changed = drop_sentence(self.TARGET, self.SOURCE, None)
        self.assertNotEqual(changed, self.TARGET)
        self.assertEqual(PLANTED["drop_sentence"][1], set())

    def test_a_join_repeat_is_planted_from_the_previous_unit(self) -> None:
        changed = repeat_join("新的单元开始了。", self.SOURCE, f"前文。{LONG_ZH}")
        self.assertTrue(changed.startswith(LONG_ZH.rstrip("。")))
        self.assertIsNone(repeat_join("新的单元开始了。", self.SOURCE, None))


def _translated_project(root: Path, targets: list[str], sources: dict[str, str] | None = None,
                        with_term: bool = False) -> dict:
    config, _ = make_project(root, with_term=with_term, unit_count=len(targets))
    for unit_id, text in (sources or {}).items():
        atomic_write_text(root / "source-work" / "work-units" / f"{unit_id}.md", text)
    record_review(root, "terminology", True, "tester", "approved")
    for number, text in enumerate(targets, 1):
        unit_id = f"unit-{number:04d}"
        atomic_write_text(root / "target" / "draft" / f"{unit_id}.md", text)
        dispatch_unit(root, unit_id)
        record_text_output(root, unit_id, "draft")
        atomic_write_text(root / "target" / "edited" / f"{unit_id}.md", text)
        record_text_output(root, unit_id, "edit")
    state = load_state(root)
    state["human_reviews"].append({"checkpoint": "translation_review", "status": "pending",
                                   "recorded_at": "2026-11-24T00:00:00Z", "notes": ""})
    save_state(root, state)
    return config


def _finding(severity: str = "major", status: str = "open") -> dict:
    return {"category": "accuracy.omission", "severity": severity, "status": status, "cause": "sentence-dropped",
            "source_excerpt": "bearing preload is 20 mm.", "target_excerpt": "", "note": "The value is missing."}


def _judge(root: Path, verdicts: dict[str, tuple[str, list]]) -> Path:
    review = load_json(root / TEMPLATE)
    review["reviewer"] = "independent-reviewer"
    for item in review["items"]:
        item["verdict"], item["findings"] = verdicts.get(item["unit_id"], ("ok", []))
    path = root / "qa" / "judged.json"
    atomic_write_json(path, review)
    return path


class TranslationReviewTests(unittest.TestCase):
    TARGETS = ["# 轴承 1\n\n轴承预紧为 10 mm。\n", "# 轴承 2\n\n轴承预紧为 20 mm。\n", "# 轴承 3\n\n轴承预紧为 30 mm。\n"]

    def test_study_samples_each_part_and_qa_needs_the_recorded_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = _translated_project(root, self.TARGETS)
            built = build_translation_review(root, config)
            template = load_json(root / TEMPLATE)
            # One part of three units at a 10% share: one unit, the middle one.
            self.assertEqual([i["unit_id"] for i in template["items"]], ["unit-0002"])
            self.assertEqual(built["pending"], 1)
            self.assertEqual(problems(root, config)[0][:2], ("translation_review.record", "fail"))
            with self.assertRaisesRegex(BTPError, "needs a reviewer"):
                record_translation_review(root, config, root / TEMPLATE)
            unjudged = load_json(root / TEMPLATE)
            unjudged["reviewer"] = "independent-reviewer"
            atomic_write_json(root / "qa" / "unjudged.json", unjudged)
            with self.assertRaisesRegex(BTPError, "pending"):
                record_translation_review(root, config, root / "qa" / "unjudged.json")

    def test_open_major_findings_block_until_fixed_and_a_change_makes_the_review_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _translated_project(root, self.TARGETS)
            # The fixture units are a few words long, so any finding puts a sample over the bar;
            # reading every unit is what clears that, and what this test uses.
            raw = load_yaml(root / "book-project.yaml")
            raw["quality"] = {"review_scope": "all"}
            write_yaml(root / "book-project.yaml", raw)
            config, _ = resolve_project(root)
            build_translation_review(root, config)
            with self.assertRaisesRegex(BTPError, "open findings"):
                record_translation_review(root, config, _judge(root, {"unit-0002": ("ok", [_finding()])}))
            record_translation_review(root, config, _judge(root, {"unit-0002": ("findings", [_finding()])}))
            self.assertIn(("translation_review.open", "fail"), [p[:2] for p in problems(root, config)])
            with self.assertRaisesRegex(BTPError, "cannot be approved"):
                record_review(root, "translation_review", True, "user", "")
            record_translation_review(root, config, _judge(root, {"unit-0002": ("findings", [_finding(status="fixed")])}))
            self.assertEqual([p[:2] for p in problems(root, config)], [("translation_review.score", "pass")])
            record_review(root, "translation_review", True, "user", "")
            self.assertTrue(load_state(root)["human_reviews"][-1]["evidence_hashes"]["translation_review"])
            # The translation changes after the review: stale, and QA says so.
            atomic_write_text(root / "target" / "edited" / "unit-0002.md", "# 轴承 2\n\n轴承预紧为 20 毫米。\n")
            self.assertIn(("translation_review.record", "fail"), [p[:2] for p in problems(root, config)])

    def test_a_sample_over_the_bar_asks_for_the_whole_part(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = _translated_project(root, self.TARGETS)
            config["quality"]["max_penalty_per_1000_words"] = 10
            build_translation_review(root, config)
            record_translation_review(root, config, _judge(root, {"unit-0002": ("findings", [_finding(status="fixed")])}))
            ids = [p[:2] for p in problems(root, config)]
            self.assertIn(("translation_review.sample", "fail"), ids)
            # The rebuilt review keeps the judged unit and its findings, and adds the rest of the stretch.
            build_translation_review(root, config)
            items = {i["unit_id"]: i for i in load_json(root / TEMPLATE)["items"]}
            self.assertEqual(sorted(items), ["unit-0001", "unit-0002", "unit-0003"])
            self.assertEqual((items["unit-0002"]["verdict"], items["unit-0002"]["findings"][0]["status"]), ("findings", "fixed"))
            self.assertEqual(items["unit-0001"]["verdict"], "pending")
            self.assertIn("its part of the book scored above the bar", items["unit-0001"]["reasons"])

    def test_rebuilding_keeps_verdicts_entered_but_not_yet_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = _translated_project(root, self.TARGETS)
            config["quality"]["review_scope"] = "all"
            build_translation_review(root, config)
            review = load_json(root / TEMPLATE)
            review["reviewer"] = "independent-reviewer"
            for item in review["items"]:
                if item["unit_id"] == "unit-0001":
                    item["verdict"] = "ok"
                if item["unit_id"] == "unit-0003":
                    item["verdict"], item["findings"] = "findings", [_finding("minor")]
            atomic_write_json(root / TEMPLATE, review)
            atomic_write_text(root / "target" / "edited" / "unit-0003.md", "# 轴承 3\n\n轴承预紧为 30 毫米。\n")
            build_translation_review(root, config)
            rebuilt = load_json(root / TEMPLATE)
            items = {i["unit_id"]: i for i in rebuilt["items"]}
            self.assertEqual(rebuilt["reviewer"], "independent-reviewer")
            self.assertEqual((items["unit-0001"]["verdict"], items["unit-0002"]["verdict"]), ("ok", "pending"))
            # Changed since it was judged: back to pending, its finding kept for the reviewer.
            self.assertEqual((items["unit-0003"]["verdict"], len(items["unit-0003"]["findings"])), ("pending", 1))

    def test_a_review_judged_in_a_copy_is_what_a_rebuild_keeps(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = _translated_project(root, self.TARGETS)
            config["quality"]["review_scope"] = "all"
            build_translation_review(root, config)
            record_translation_review(root, config, _judge(root, {"unit-0002": ("findings", [_finding()])}))
            atomic_write_text(root / "target" / "edited" / "unit-0002.md", "# 轴承 2\n\n轴承预紧为 20 毫米。\n")
            build_translation_review(root, config)  # unit-0002 back to pending with its open finding
            # The reviewer fixes it in a copy of the review file and records the copy.
            copy = load_json(root / TEMPLATE)
            for item in copy["items"]:
                if item["unit_id"] == "unit-0002":
                    item["verdict"], item["findings"][0]["status"] = "findings", "fixed"
            atomic_write_json(root / "qa" / "copy.json", copy)
            record_translation_review(root, config, root / "qa" / "copy.json")
            build_translation_review(root, config)
            item = next(i for i in load_json(root / TEMPLATE)["items"] if i["unit_id"] == "unit-0002")
            self.assertEqual((item["verdict"], item["findings"][0]["status"]), ("findings", "fixed"))

    def test_table_cells_count_as_source_words(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = _translated_project(root, self.TARGETS)
            config["quality"]["review_scope"] = "all"
            source = root / "source-work" / "work-units" / "unit-0001.md"
            atomic_write_text(source, "| Process step | Cycle time in minutes |\n|---|---|\n"
                                      "| Cutting the steel bar | Twelve |\n")
            build_translation_review(root, config)
            words = {i["unit_id"]: i["source_words"] for i in load_json(root / TEMPLATE)["items"]}
            # Every cell's words, header included (it counted 0 when only prose was weighed).
            self.assertEqual(words["unit-0001"], 11)

    def test_a_checkpoint_without_a_review_scope_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = _translated_project(root, self.TARGETS)
            config["quality"]["review_scope"] = "none"
            [(check_id, status, message)] = problems(root, config)
            self.assertEqual((check_id, status), ("translation_review.record", "fail"))
            self.assertIn("review_scope is none", message)

    def test_show_unit_gives_the_reviewer_the_selected_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = _translated_project(root, self.TARGETS)
            with self.assertRaisesRegex(BTPError, "Run translation-review"):
                show_unit(root, "unit-0002", "review")
            build_translation_review(root, config)
            package = show_unit(root, "unit-0002", "review")
            self.assertEqual(Path(package["target_file"]), root / "target" / "edited" / "unit-0002.md")
            self.assertEqual(Path(package["review_file"]), root / TEMPLATE)
            self.assertEqual((package["review_reasons"], package["earlier_findings"]),
                             (["sample of its part of the book"], []))
            self.assertEqual(package["neighbour_targets"],
                             {"previous_end": self.TARGETS[0].strip(), "next_start": self.TARGETS[2].strip()})
            with self.assertRaisesRegex(BTPError, "not among the units selected"):
                show_unit(root, "unit-0001", "review")

    def test_units_with_no_reader_text_are_not_selected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = _translated_project(root, self.TARGETS + ["<!-- 原书索引已略去，见译者注。 -->\n"])
            config["quality"]["review_scope"] = "all"
            build_translation_review(root, config)
            review = load_json(root / TEMPLATE)
            self.assertEqual([i["unit_id"] for i in review["items"]], ["unit-0001", "unit-0002", "unit-0003"])
            self.assertEqual(sum(c["units"] for c in review["chapters"].values()), 3)

    def test_a_sample_pick_on_a_placeholder_moves_to_its_nearest_readable_neighbour(self) -> None:
        targets = [f"# 轴承 {n}\n\n轴承预紧为 {n * 10} mm。\n" for n in range(1, 11)]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = _translated_project(root, targets)
            build_translation_review(root, config)
            self.assertEqual([i["unit_id"] for i in load_json(root / TEMPLATE)["items"]], ["unit-0006"])
        targets[5] = "<!-- 本单元内容已在 unit-0007 中重建。 -->\n"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = _translated_project(root, targets)
            build_translation_review(root, config)
            self.assertEqual([i["unit_id"] for i in load_json(root / TEMPLATE)["items"]], ["unit-0005"])

    def test_a_hyphenated_glossary_term_missing_from_the_translation_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = _translated_project(root, ["# 轴承 1\n\n预紧为 10 mm。\n", *self.TARGETS[1:]],
                                         {"unit-0001": "# Bearing 1\n\nbearing pre-load is 10 mm.\n"}, with_term=True)
            checks = run_qa(root, config)["checks"]
            self.assertEqual([c["unit_id"] for c in checks if c["id"] == "terminology.hyphenated"], ["unit-0001"])
            # The selection (a dependency) is unchanged, so the unit's approved-term check does not fail.
            self.assertNotIn(("terminology.approved", "unit-0001"),
                             [(c["id"], c.get("unit_id")) for c in checks if c["status"] == "fail"])

    def test_publication_reviews_every_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = _translated_project(root, self.TARGETS)
            config["quality"]["review_scope"] = "all"
            build_translation_review(root, config)
            self.assertEqual(len(load_json(root / TEMPLATE)["items"]), 3)

    def test_qa_runs_the_review_checks_and_the_rule_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = _translated_project(root, self.TARGETS)
            ids = {item["id"] for item in run_qa(root, config)["checks"]}
            self.assertIn("translation_review.record", ids)
            self.assertTrue((root / "qa" / "translation-signals.json").is_file())
            self.assertTrue((root / "terminology" / "ledger.json").is_file())

    def test_score_weights_findings_per_thousand_source_words(self) -> None:
        review = {"chapters": {"A": {"units": 2, "source_words": 2000}}, "items": [
            {"unit_id": "unit-0001", "chapter": "A", "source_words": 500, "findings": [
                _finding("minor", "open"), _finding("major", "fixed"), _finding("critical", "dismissed")]},
        ]}
        result = score(review, 10)
        self.assertEqual((result["found_per_1000"], result["open_per_1000"]), (12.0, 2.0))
        self.assertTrue(result["chapters"]["A"]["needs_full_review"])
        self.assertEqual(result["blocking_open"], [])


class ReviewLessonCheckTests(unittest.TestCase):
    """Checks added after the first real review (references/translation-quality.md)."""

    def test_a_sentence_left_as_fragments_across_a_join_is_reported(self) -> None:
        units = [_unit("unit-0001", "The chart shows the trend of manual entries made after", "> 会议上展示趋势图……\n"),
                 _unit("unit-0002", "month-end close. We assign one person.", "> ……月结后编制的人工录入数量。\n")]
        self.assertEqual([(f["unit_id"], f["where"]) for f in split_fragments(units)],
                         [("unit-0001", "end"), ("unit-0002", "start")])

    def test_an_ellipsis_the_source_has_is_left_alone(self) -> None:
        units = [_unit("unit-0001", "He paused and said, “Well . . .”", "他停顿了一下，说：“嗯……”\n"),
                 _unit("unit-0002", "...and then the line stopped.", "……然后生产线停了。\n"),
                 _unit("unit-0003", "Lists go on. Value, flow, pull…", "清单还在继续：价值、流动、拉动……\n")]
        self.assertEqual(split_fragments(units), [])

    def test_a_paragraph_marked_as_a_heading_is_reported(self) -> None:
        paragraph = "## " + "第 7 章讨论传统标准成本法给正在走向精益转型的企业带来的严重影响，" * 2 + "这一点十分重要。\n"
        self.assertEqual(len(heading_paragraphs(paragraph)), 1)
        quoted = "## " + "他说道：" + "精益转型需要每个人都参与进来并持续改善流程，" * 3 + "我从没见过他这么兴奋！”\n"
        self.assertEqual(len(heading_paragraphs(quoted)), 1)

    def test_real_headings_are_left_alone(self) -> None:
        headings = ("# 1　精益困境：选择系统原则，还是管理会计控制——不能两者兼得\n\n"
                    "## 3.3　实施精益战略需要什么？\n\n### （l）准备回答“这对我有什么好处？”\n\n"
                    "## 1.2 Business Results: Mechanism Versus Life System.\n")
        self.assertEqual(heading_paragraphs(headings), [])

    def test_dropped_note_markers_are_reported(self) -> None:
        units = [_unit("unit-0001", "They see it.^7^ Accountants agree.^8^ Fewer are needed.^9^",
                       "他们看到了这一点。会计师同意。所需人员会减少。")]
        self.assertEqual(note_marker_gaps(units), [{"unit_id": "unit-0001", "source": 3, "target": 0}])

    def test_kept_moved_and_look_alike_markers(self) -> None:
        units = [
            # Kept as a digit after full-width punctuation, a superscript, or a note reference.
            _unit("unit-0001", "It grew.^4^ It fell.^5^ It held.^6^", "它增长了。4它下降了。⁵它保持不变。[^6]"),
            # Moved to the next unit with its sentence.
            _unit("unit-0002", "Costs fell sharply.^7^", "成本大幅下降，"),
            _unit("unit-0003", "Then the plant grew.", "这在当年就已显现。7随后工厂扩大了。"),
        ]
        self.assertEqual(note_marker_gaps(units), [])
        # A note's own text starting with its number is not a second reference.
        self.assertEqual(note_marker_gaps([_unit("unit-0001", "It grew. ^1^ More.\n\n^1^I am indebted to Esa Saarinen.",
                                                 "它增长了。[^1] 更多。\n\n[^1]: 感谢 Esa Saarinen。")]), [])
        # A year or a percentage after punctuation is not a marker.
        self.assertEqual(note_marker_gaps([_unit("unit-0001", "Sales rose.^1^", "销售增长，2005 年为 12%。")]),
                         [{"unit_id": "unit-0001", "source": 1, "target": 0}])


class RuleProposalTests(unittest.TestCase):
    def test_causes_recurring_across_books_are_proposed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            books = []
            for name, causes in (("book-a", ["term-not-in-glossary"] * 2 + ["sentence-dropped"]),
                                 ("book-b", ["term-not-in-glossary", "false-friend"])):
                project = root / name
                findings = [dict(_finding("major", "fixed"), category="terminology.wrong_term", cause=cause)
                            if cause != "sentence-dropped" else _finding("minor", "fixed") for cause in causes]
                findings.append(dict(_finding(), cause="not-an-error", status="dismissed"))
                atomic_write_json(project / RECORD, {"items": [{"unit_id": "unit-0001", "findings": findings}]})
                books.append(project)
            result = rule_proposals(books, min_count=3, output=root)
            self.assertEqual(result["proposed"], ["term-not-in-glossary"])
            report = json.loads((root / "rule-proposals.json").read_text(encoding="utf-8"))
            top = report["groups"][0]
            self.assertEqual((top["count"], top["books"]), (3, ["book-a", "book-b"]))
            self.assertEqual(top["existing_checks"], ["terminology.approved"])
            self.assertNotIn("not-an-error", [g["cause"] for g in report["groups"]])

    def test_a_cause_is_counted_across_categories(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            findings = [dict(_finding("minor", "fixed"), category=category, cause="term-not-in-glossary")
                        for category in ("terminology.wrong_term", "terminology.inconsistent", "terminology.inconsistent")]
            atomic_write_json(root / "book" / RECORD, {"items": [{"unit_id": "unit-0001", "findings": findings}]})
            result = rule_proposals([root / "book"], min_count=3, output=root)
            self.assertEqual(result["proposed"], ["term-not-in-glossary"])
            group = json.loads((root / "rule-proposals.json").read_text(encoding="utf-8"))["groups"][0]
            self.assertEqual(group["categories"], ["terminology.wrong_term", "terminology.inconsistent"])
            self.assertEqual(group["existing_checks"],
                             ["terminology.approved", "terminology.ledger", "terminology.name_consistency"])


class ConfigTests(unittest.TestCase):
    def _resolve(self, mode: str) -> dict:
        return resolve_config({"schema_version": 2, "project_id": "q", "mode": mode, "outputs": ["markdown"],
                               "workspace": ".", "source": {"path": "s.pdf", "language": "en"},
                               "target": {"language": "zh-Hans"}})

    def test_modes_set_the_review(self) -> None:
        fast, study, publication = (self._resolve(m) for m in ("fast", "study", "publication"))
        self.assertEqual(fast["quality"]["review_scope"], "none")
        self.assertNotIn("translation_review", fast["execution"]["checkpoints"])
        self.assertEqual((study["quality"]["review_scope"], study["quality"]["max_penalty_per_1000_words"]), ("sample", 10))
        self.assertIn("translation_review", study["execution"]["checkpoints"])
        self.assertEqual((publication["quality"]["review_scope"], publication["quality"]["max_penalty_per_1000_words"]),
                         ("all", 5))

    def test_recording_an_observation_rejects_an_unusable_new_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_project(root)
            bad = root / "obs.json"
            atomic_write_json(bad, {"schema_version": 1, "new_entries": [{"source": "Deming", "target": ""}],
                                    "conflicts": [], "source_issues": [], "used_glossary_ids": []})
            self.assertEqual(main(["record", str(root), "--kind", "observation", "--unit", "unit-0001",
                                   "--input", str(bad)]), 2)


if __name__ == "__main__":
    unittest.main()
