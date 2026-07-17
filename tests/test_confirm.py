"""The --delete confirmation decider (confirm.py) via the console prompt seam."""

from __future__ import annotations

import json
import os

import pytest

from s3bak import confirm
from s3bak.confirm import AnswerMode, DeleteConfirmer, DeletionAbortedError


@pytest.fixture(autouse=True)
def _fresh_abort_state():
    confirm.reset_confirmations()
    yield
    confirm.reset_confirmations()


def _kept_lines(confirmer: DeleteConfirmer) -> list[str]:
    path = confirmer.kept_keys_path()
    if path is None:
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def test_resolve_answer_mode_matrix(monkeypatch):
    assert confirm.resolve_answer_mode(yes=True) is AnswerMode.ALL_YES
    monkeypatch.setattr(confirm, "prompt_is_interactive", lambda: True)
    assert confirm.resolve_answer_mode(yes=False) is AnswerMode.ASK
    monkeypatch.setattr(confirm, "prompt_is_interactive", lambda: False)
    assert confirm.resolve_answer_mode(yes=False) is AnswerMode.ALL_NO


def test_non_interactive_is_the_default_under_pytest():
    # pytest's stdin is not a TTY, so the auto-n path needs no monkeypatching.
    assert confirm.resolve_answer_mode(yes=False) is AnswerMode.ALL_NO


def test_all_yes_deletes_without_prompting(answers):
    confirmer = DeleteConfirmer(AnswerMode.ALL_YES, "data")
    try:
        assert confirmer.confirm("x", kept_key="x") is True
        assert answers.prompts == []
        assert confirmer.kept_keys_path() is None
    finally:
        confirmer.close()


def test_all_no_keeps_and_records_without_prompting(answers):
    confirmer = DeleteConfirmer(AnswerMode.ALL_NO, "data")
    try:
        assert confirmer.confirm("x", kept_key="sub/x.txt") is False
        assert confirmer.confirm("y", kept_key="sub/y.txt") is False
        assert answers.prompts == []
        assert _kept_lines(confirmer) == ["sub/x.txt", "sub/y.txt"]
    finally:
        confirmer.close()


def test_interactive_y_n_sequencing(answers):
    answers.feed("y", "n")
    confirmer = DeleteConfirmer(AnswerMode.ASK, "data")
    try:
        assert confirmer.confirm("a.txt", kept_key="a.txt") is True
        assert confirmer.confirm("b.txt", kept_key="b.txt") is False
        assert _kept_lines(confirmer) == ["b.txt"]
        assert len(answers.prompts) == 2
        assert "data" in answers.prompts[0]
        assert "a.txt" in answers.prompts[0]
    finally:
        confirmer.close()


def test_answer_a_deletes_everything_after(answers):
    answers.feed("a")
    confirmer = DeleteConfirmer(AnswerMode.ASK, "data")
    try:
        assert confirmer.confirm("a.txt", kept_key="a.txt") is True
        assert confirmer.confirm("b.txt", kept_key="b.txt") is True
        assert len(answers.prompts) == 1
        assert confirmer.kept_keys_path() is None
    finally:
        confirmer.close()


def test_answer_d_keeps_everything_after(answers):
    answers.feed("d")
    confirmer = DeleteConfirmer(AnswerMode.ASK, "data")
    try:
        assert confirmer.confirm("a.txt", kept_key="a.txt") is False
        assert confirmer.confirm("b.txt", kept_key="b.txt") is False
        assert len(answers.prompts) == 1
        assert _kept_lines(confirmer) == ["a.txt", "b.txt"]
    finally:
        confirmer.close()


def test_invalid_and_empty_answers_print_the_legend_and_reprompt(answers, capfd):
    # A bare Enter must re-ask, never abort: only EOF (None) counts as q.
    answers.feed("", "maybe", "y")
    confirmer = DeleteConfirmer(AnswerMode.ASK, "data")
    try:
        assert confirmer.confirm("a.txt") is True
        assert len(answers.prompts) == 3
        assert capfd.readouterr().err.count("q, quit - abort the whole command") == 2
    finally:
        confirmer.close()


def test_question_mark_prints_the_legend(answers, capfd):
    answers.feed("?", "n")
    confirmer = DeleteConfirmer(AnswerMode.ASK, "data")
    try:
        assert confirmer.confirm("a.txt", kept_key="a.txt") is False
        assert len(answers.prompts) == 2
        assert "[y/n/a/d/q/?]" in answers.prompts[0]
        assert "y, yes  - delete this one" in capfd.readouterr().err
    finally:
        confirmer.close()


def test_full_word_answers(answers):
    answers.feed("yes", "no", "all")
    confirmer = DeleteConfirmer(AnswerMode.ASK, "data")
    try:
        assert confirmer.confirm("a.txt", kept_key="a.txt") is True
        assert confirmer.confirm("b.txt", kept_key="b.txt") is False
        assert confirmer.confirm("c.txt", kept_key="c.txt") is True
        assert confirmer.confirm("d.txt", kept_key="d.txt") is True  # all is sticky
        assert len(answers.prompts) == 3
        assert _kept_lines(confirmer) == ["b.txt"]
    finally:
        confirmer.close()


def test_quit_word_aborts(answers):
    answers.feed("quit")
    confirmer = DeleteConfirmer(AnswerMode.ASK, "data")
    try:
        with pytest.raises(DeletionAbortedError):
            confirmer.confirm("a.txt")
    finally:
        confirmer.close()


def test_delete_word_is_not_an_answer(answers, capfd):
    # d means keep, so the word "delete" must never be accepted as an answer;
    # like any unrecognized input it prints the legend and re-asks.
    answers.feed("delete", "n")
    confirmer = DeleteConfirmer(AnswerMode.ASK, "data")
    try:
        assert confirmer.confirm("a.txt", kept_key="a.txt") is False
        assert len(answers.prompts) == 2
        assert "d       - keep this one" in capfd.readouterr().err
    finally:
        confirmer.close()


def test_answer_summary_prints_once_per_run(answers, capfd):
    answers.feed("y", "y")
    confirmer = DeleteConfirmer(AnswerMode.ASK, "data")
    try:
        assert confirmer.confirm("a.txt") is True
        assert confirmer.confirm("b.txt") is True
    finally:
        confirmer.close()
    assert capfd.readouterr().err.count("--delete answers:") == 1
    confirm.reset_confirmations()  # a new run reprints the summary
    answers.feed("y")
    fresh = DeleteConfirmer(AnswerMode.ASK, "data")
    try:
        assert fresh.confirm("a.txt") is True
    finally:
        fresh.close()
    assert "--delete answers:" in capfd.readouterr().err


def test_q_aborts_this_and_every_later_confirmation(answers):
    answers.feed("q")
    confirmer = DeleteConfirmer(AnswerMode.ASK, "data")
    other = DeleteConfirmer(AnswerMode.ASK, "other-entry")
    try:
        with pytest.raises(DeletionAbortedError):
            confirmer.confirm("a.txt", kept_key="a.txt")
        # A sibling --all entry stops without prompting.
        with pytest.raises(DeletionAbortedError):
            other.confirm("b.txt", kept_key="b.txt")
        assert len(answers.prompts) == 1
    finally:
        confirmer.close()
        other.close()


def test_eof_mid_prompt_aborts(answers):
    # No queued answer -> the fake seam returns "" like a closed stdin.
    confirmer = DeleteConfirmer(AnswerMode.ASK, "data")
    try:
        with pytest.raises(DeletionAbortedError):
            confirmer.confirm("a.txt")
    finally:
        confirmer.close()


def test_reset_confirmations_clears_the_abort_flag(answers):
    answers.feed("q")
    confirmer = DeleteConfirmer(AnswerMode.ASK, "data")
    try:
        with pytest.raises(DeletionAbortedError):
            confirmer.confirm("a.txt")
    finally:
        confirmer.close()
    confirm.reset_confirmations()
    answers.feed("y")
    fresh = DeleteConfirmer(AnswerMode.ASK, "data")
    try:
        assert fresh.confirm("a.txt") is True
    finally:
        fresh.close()


def test_kept_file_is_removed_on_close(answers):
    confirmer = DeleteConfirmer(AnswerMode.ALL_NO, "data")
    confirmer.confirm("x", kept_key="x")
    path = confirmer.kept_keys_path()
    assert path is not None and os.path.exists(path)
    confirmer.close()
    assert not os.path.exists(path)
    confirmer.close()  # idempotent


def test_kept_keys_survive_newlines_in_filenames(answers):
    confirmer = DeleteConfirmer(AnswerMode.ALL_NO, "data")
    try:
        confirmer.confirm("x", kept_key="evil\nname.txt")
        confirmer.confirm("y", kept_key="plain.txt")
        assert _kept_lines(confirmer) == ["evil\nname.txt", "plain.txt"]
    finally:
        confirmer.close()


def test_confirm_subtree_delete_modes(answers, capfd):
    assert confirm.confirm_subtree_delete(AnswerMode.ALL_YES, "data", "sub") is True
    assert confirm.confirm_subtree_delete(AnswerMode.ALL_NO, "data", "sub") is False
    assert answers.prompts == []
    answers.feed("y")
    assert confirm.confirm_subtree_delete(AnswerMode.ASK, "data", "sub") is True
    answers.feed("no")
    assert confirm.confirm_subtree_delete(AnswerMode.ASK, "data", "sub") is False
    answers.feed("bogus", "")  # unrecognized answers explain and re-ask; EOF answers n
    assert confirm.confirm_subtree_delete(AnswerMode.ASK, "data", "sub") is False
    assert "data" in answers.prompts[0]
    assert capfd.readouterr().err.count("keep it") == 2
