from __future__ import annotations

from centaur_pocket.im_knowledge import extract_knowledge_candidates


def message(message_id: str, text: str) -> dict:
    return {
        "id": message_id,
        "conversation_id": "conv-1",
        "sender_display_name": "老板",
        "text_content": text,
    }


def test_explicit_decisions_commitments_and_tasks_are_provisional() -> None:
    candidates = extract_knowledge_candidates(
        [
            message("m1", "决定下周一发布。"),
            message("m2", "我来负责最终验收。"),
            message("m3", "请你明天下午跟进合同。"),
        ]
    )

    assert [candidate.claim_type for candidate in candidates] == [
        "decision",
        "commitment",
        "task",
    ]
    assert all(candidate.status == "provisional" for candidate in candidates)
    assert all(candidate.explicitness == "explicit" for candidate in candidates)
    assert all(candidate.message_id for candidate in candidates)


def test_negated_or_ambiguous_chat_does_not_become_a_candidate() -> None:
    assert extract_knowledge_candidates(
        [
            message("m1", "还没决定下周是否发布。"),
            message("m2", "也许可以看看。"),
            message("m3", "不需要你安排。"),
        ]
    ) == []


def test_same_words_in_distinct_messages_keep_distinct_evidence() -> None:
    candidates = extract_knowledge_candidates(
        [message("m1", "我会处理。"), message("m2", "我会处理。")]
    )

    assert len(candidates) == 2
    assert candidates[0].idempotency_key != candidates[1].idempotency_key
