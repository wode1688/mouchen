from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from app.domain.models import (
    AdviceLevel,
    AdviceRecord,
    AdviceStatus,
    ContextSnapshot,
    Event,
    FeedbackCreate,
    Goal,
    OutcomeCreate,
    OutcomeStatus,
)
from app.domain.problem_signals import PROBLEM_RULES, detect_problem
from app.service import ProactiveService
from app.storage import Repository


def make_service(tmp_path: Path):
    repo = Repository(tmp_path / "test.db")
    service = ProactiveService(repo)
    goal = repo.insert_goal(
        Goal(
            user_id="u1",
            domain="work",
            title="发布产品",
            quote="本周最重要的是发布产品",
            target={"weekly_hours": 10},
        )
    )
    repo.set_charter("u1", "work", AdviceLevel.L4, True)
    return repo, service, goal


def test_time_drift_creates_auditable_advice(tmp_path):
    repo, service, goal = make_service(tmp_path)
    result = service.ingest(
        Event(
            user_id="u1",
            source="usage",
            type="time.allocation",
            facts={"domain": "work", "actual_hours": 1, "expected_hours": 10},
            confidence=0.98,
        )
    )
    assert result is not None
    assert result.decision == "publish"
    assert result.effective_level == AdviceLevel.L2
    assert result.advice is not None
    assert result.advice.goal_quote == goal.quote
    assert result.advice.alternative
    assert "累计投入达到 10.0 小时" in result.advice.adopted_expected_result
    assert result.advice.adopted_confidence is not None
    repo.close()


def test_multiple_current_goals_in_same_domain_are_preserved(tmp_path):
    repo = Repository(tmp_path / "parallel-goals.db")
    first = repo.insert_goal(
        Goal(
            user_id="u1",
            domain="work",
            title="发布AI替身",
            quote="本周发布AI替身内测版",
            target={"keywords": ["AI替身"]},
        )
    )
    second = repo.insert_goal(
        Goal(
            user_id="u1",
            domain="work",
            title="完成客户交付",
            quote="本周完成客户交付验收",
            target={"keywords": ["客户", "验收"]},
        )
    )

    current_ids = {goal.id for goal in repo.current_goals("u1")}

    assert current_ids == {first.id, second.id}
    repo.close()


def test_expired_goal_is_not_used_for_new_advice(tmp_path):
    repo = Repository(tmp_path / "expired-goal.db")
    repo.insert_goal(
        Goal(
            user_id="u1",
            domain="work",
            title="旧目标",
            quote="昨天以前完成旧目标",
            valid_until=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
    )

    assert repo.current_goals("u1") == []
    repo.close()


def test_verified_l2_outcomes_can_earn_l3_speaking_rights(tmp_path):
    repo, service, goal = make_service(tmp_path)
    now = datetime.now(timezone.utc)
    for index in range(20):
        advice = AdviceRecord(
            user_id="u1",
            domain="work",
            requested_level=AdviceLevel.L2,
            effective_level=AdviceLevel.L2,
            goal_id=goal.id,
            goal_quote=goal.quote,
            evidence=[
                {
                    "event_id": uuid4(),
                    "source": "test",
                    "fact": f"第 {index + 1} 条已核验事实",
                    "observed_at": now,
                    "confidence": 0.99,
                }
            ],
            action="完成一个可核验动作",
            first_step="执行第一步",
            alternative="采用备用路径",
            prediction={
                "outcome": "动作按期完成",
                "deadline": now + timedelta(days=1),
                "confidence": 0.90,
            },
            adopted_expected_result="采纳后动作按期完成",
            adopted_confidence=0.90,
            urgency=0.8,
            impact=0.8,
            novelty=0.8,
            relevance=1.0,
            context_fit=1.0,
            interruption_cost=0.1,
            dedupe_key=f"earned-l2-{index}",
            proactive_score=0.9,
            delivery="immediate",
            status=AdviceStatus.ADOPTED,
        )
        repo.insert_advice(advice)
        repo.record_outcome(
            "u1",
            advice,
            OutcomeCreate(status=OutcomeStatus.CORRECT, actual_result="结果达成"),
        )

    result = service.ingest(
        Event(
            user_id="u1",
            source="usage",
            type="time.allocation",
            facts={"domain": "work", "actual_hours": 1, "expected_hours": 10},
            confidence=0.98,
        )
    )

    assert result is not None
    assert result.advice is not None
    assert result.advice.requested_level == AdviceLevel.L3
    assert result.effective_level == AdviceLevel.L3
    repo.close()


def test_duplicate_merges_into_active_thread(tmp_path):
    repo, service, _ = make_service(tmp_path)
    first = Event(user_id="u1", source="usage", type="time.allocation", facts={"domain":"work","actual_hours":1,"expected_hours":10}, confidence=0.98)
    second = Event(user_id="u1", source="usage", type="time.allocation", facts={"domain":"work","actual_hours":2,"expected_hours":10}, confidence=0.98)
    assert service.ingest(first).decision == "publish"
    assert service.ingest(second).decision == "merge"
    repo.close()


def test_expired_active_advice_is_withdrawn_and_releases_duplicate_key(tmp_path):
    repo, service, _ = make_service(tmp_path)
    first = service.ingest(
        Event(
            user_id="u1",
            source="usage",
            type="time.allocation",
            facts={"domain": "work", "actual_hours": 3, "expected_hours": 10},
            confidence=0.98,
        )
    )
    expired_prediction = first.advice.prediction.model_copy(
        update={"deadline": datetime.now(timezone.utc) - timedelta(seconds=1)}
    )
    repo.update_advice(first.advice.model_copy(update={"prediction": expired_prediction}))

    assert repo.active_duplicate("u1", first.advice.dedupe_key) is False
    expired = repo.get_advice("u1", first.advice.id)
    assert expired.status == AdviceStatus.WITHDRAWN
    row = repo._connection.execute(
        "SELECT status, payload_json FROM advice WHERE id=?",
        (str(first.advice.id),),
    ).fetchone()
    assert row["status"] == AdviceStatus.WITHDRAWN.value
    assert AdviceStatus.WITHDRAWN.value in row["payload_json"]

    # Clear the per-goal preference cooldown left by the first publish.
    repo._connection.execute(
        "UPDATE advice SET published_at=? WHERE published_at IS NOT NULL",
        ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),),
    )
    repo._connection.commit()

    replacement = service.ingest(
        Event(
            user_id="u1",
            source="usage",
            type="time.allocation",
            facts={"domain": "work", "actual_hours": 4, "expected_hours": 10},
            confidence=0.98,
        )
    )
    assert replacement.decision == "publish"
    assert replacement.advice.status == AdviceStatus.ACTIVE
    assert [item.status for item in repo.list_advice("u1")] == [
        AdviceStatus.ACTIVE,
        AdviceStatus.WITHDRAWN,
    ]
    repo.close()


def test_expiry_sweep_preserves_resolved_advice_statuses(tmp_path):
    repo, service, _ = make_service(tmp_path)
    generated = service.ingest(
        Event(
            user_id="u1",
            source="usage",
            type="time.allocation",
            facts={"domain": "work", "actual_hours": 1, "expected_hours": 10},
            confidence=0.98,
        )
    ).advice
    past_prediction = generated.prediction.model_copy(
        update={"deadline": datetime.now(timezone.utc) - timedelta(days=1)}
    )
    expected = {
        AdviceStatus.ADOPTED,
        AdviceStatus.DISMISSED,
        AdviceStatus.VERIFIED,
    }
    for status in expected:
        repo.insert_advice(
            generated.model_copy(
                update={
                    "id": uuid4(),
                    "dedupe_key": f"resolved:{status.value}",
                    "status": status,
                    "prediction": past_prediction,
                    "evidence": [
                        generated.evidence[0].model_copy(update={"event_id": uuid4()})
                    ],
                }
            )
        )

    statuses = {item.status for item in repo.list_advice("u1")}

    assert expected.issubset(statuses)
    for status in expected:
        row = repo._connection.execute(
            "SELECT status, payload_json FROM advice WHERE dedupe_key=?",
            (f"resolved:{status.value}",),
        ).fetchone()
        assert row["status"] == status.value
        assert AdviceRecord.model_validate_json(row["payload_json"]).status == status
    repo.close()


def test_retried_mobile_event_is_idempotent(tmp_path):
    repo, service, _ = make_service(tmp_path)
    first = Event(
        user_id="u1",
        source="android.usage",
        type="time.allocation",
        evidence_ref="android:event-123",
        facts={"domain": "work", "actual_hours": 3, "expected_hours": 10},
        confidence=0.98,
    )
    retry = Event(
        user_id="u1",
        source=first.source,
        type=first.type,
        evidence_ref=first.evidence_ref,
        facts=first.facts,
        confidence=first.confidence,
    )

    assert service.ingest(first).decision == "publish"
    assert service.ingest(retry) is None
    assert len(repo.list_advice("u1")) == 1
    repo.close()


def test_mobile_goal_sync_is_idempotent(tmp_path):
    repo = Repository(tmp_path / "test.db")
    goal = Goal(
        user_id="u1",
        domain="work",
        title="发布产品",
        quote="本周最重要的是发布产品",
        target={"weekly_hours": 10},
    )

    first = repo.insert_goal(goal)
    retry = repo.insert_goal(goal)

    assert retry.id == first.id
    assert retry.version == first.version
    assert len(repo.list_goals("u1")) == 1
    repo.close()


def test_raw_external_intel_does_not_invent_a_threat(tmp_path):
    repo, service, _ = make_service(tmp_path)
    result = service.ingest(
        Event(
            user_id="u1",
            source="rss.industry",
            type="intel.external",
            facts={
                "domain": "work",
                "title": "关键政策即将生效",
                "summary": "关键政策将在本月生效",
                "relevance": 0.95,
                "primary_source": True,
            },
            confidence=0.95,
        )
    )

    assert result is None
    assert repo.list_advice("u1") == []
    repo.close()


def test_external_intel_with_explicit_threat_evidence_is_detected(tmp_path):
    repo, service, goal = make_service(tmp_path)
    result = service.ingest(
        Event(
            user_id="u1",
            source="rss.industry",
            type="intel.external",
            facts={
                "domain": "work",
                "title": "关键政策即将生效",
                "summary": "关键政策将在本月生效",
                "threat_evidence": "政策原文明确要求本月完成整改",
                "relevance": 0.95,
                "primary_source": True,
            },
            confidence=0.95,
        )
    )

    assert result is not None
    assert result.decision == "publish"
    assert result.advice is not None
    assert result.advice.goal_id == goal.id
    assert "完成原始来源核验" in result.advice.adopted_expected_result
    assert result.advice.adopted_confidence is not None
    repo.close()


def test_every_rule_based_problem_has_a_concrete_adopted_result_prediction():
    cases = (
        ("检测到可疑登录，若非本人请立即处理", "work"),
        ("支付失败，请核对账单", "work"),
        ("截止明天，请及时提交", "work"),
        ("航班取消，请查看替代安排", "work"),
        ("部署失败，连接超时", "work"),
        ("出现胸痛和呼吸困难", "health"),
    )
    assert len(cases) == len(PROBLEM_RULES)

    for index, (text, domain) in enumerate(cases):
        goal = Goal(
            user_id="u1",
            domain=domain,
            title=f"目标 {index}",
            quote=f"及时处理 {domain} 领域的重要问题",
        )
        candidate = detect_problem(
            Event(
                user_id="u1",
                source="android.notification",
                type="notification.posted",
                facts={"title": text},
                confidence=0.96,
            ),
            goal,
            relevance=1.0,
        )

        assert candidate is not None
        assert candidate.adopted_expected_result is not None
        assert len(candidate.adopted_expected_result) >= 20
        assert candidate.adopted_confidence is not None
        assert 0.5 <= candidate.adopted_confidence <= 0.95


def test_relevant_mail_without_threat_evidence_stays_informational(tmp_path):
    repo, service, _ = make_service(tmp_path)
    result = service.ingest(
        Event(
            user_id="u1",
            source="imap.work",
            type="mail.received",
            facts={
                "domain": "work",
                "subject": "项目周报",
                "relevance": 0.95,
            },
            confidence=0.95,
        )
    )

    assert result is None
    assert repo.list_advice("u1") == []
    repo.close()


def test_raw_usage_requires_period_aggregate_before_derivation(tmp_path):
    repo, service, _ = make_service(tmp_path)
    single_session = Event(
        user_id="u1",
        source="android.usage",
        type="app.foreground_session",
        facts={"domain": "work", "duration_ms": 60_000, "expected_hours": 10},
    )

    assert service.ingest(single_session) is None
    assert repo.list_advice("u1") == []
    repo.close()


def test_period_usage_aggregate_is_derived_into_time_allocation(tmp_path):
    repo, service, _ = make_service(tmp_path)
    result = service.ingest(
        Event(
            user_id="u1",
            source="android.usage",
            type="app.foreground_session",
            facts={
                "domain": "work",
                "period_total_duration_ms": 3_600_000,
                "expected_hours": 10,
            },
            confidence=0.95,
        )
    )

    assert result is not None
    assert result.decision == "publish"
    repo.close()


def test_outcome_updates_domain_trust(tmp_path):
    repo, service, _ = make_service(tmp_path)
    result = service.ingest(Event(user_id="u1", source="task", type="commitment.slipped", facts={"domain":"work","reschedule_count":3}, confidence=0.95))
    assert result and result.advice
    repo.record_outcome(
        "u1",
        result.advice,
        OutcomeCreate(status=OutcomeStatus.CORRECT, actual_result="再次延期", utility=0.8, timing_quality=0.9),
    )
    trust = repo.trust_summary("u1", "work", result.advice.effective_level)
    assert trust.judged == 1
    assert trust.correct == 1
    expected_brier = (result.advice.prediction.confidence - 1.0) ** 2
    assert abs(trust.brier - expected_brier) < 1e-12
    repo.close()


def test_adopted_outcome_calibrates_expected_result_not_untreated_risk(tmp_path):
    repo, service, _ = make_service(tmp_path)
    result = service.ingest(
        Event(
            user_id="u1",
            source="task",
            type="commitment.slipped",
            facts={"domain": "work", "reschedule_count": 2},
            confidence=0.95,
        )
    )
    assert result and result.advice
    advice = result.advice.model_copy(
        update={
            "prediction": result.advice.prediction.model_copy(
                update={"confidence": 0.95}
            ),
            "adopted_expected_result": "拆分后的小交付按期完成",
            "adopted_confidence": 0.60,
        }
    )
    repo.update_advice(advice)
    repo.record_feedback("u1", advice.id, FeedbackCreate(kind="adopted"))
    adopted = repo.get_advice("u1", advice.id)
    assert adopted is not None and adopted.status == AdviceStatus.ADOPTED

    repo.record_outcome(
        "u1",
        adopted,
        OutcomeCreate(status=OutcomeStatus.CORRECT, actual_result="小交付已完成"),
    )

    trust = repo.trust_summary("u1", "work", adopted.effective_level)
    assert trust.judged == 1
    assert trust.correct == 1
    assert abs(trust.brier - (0.60 - 1.0) ** 2) < 1e-12
    assert abs(trust.brier - (0.95 - 1.0) ** 2) > 1e-3
    repo.close()


def test_legacy_adopted_outcome_without_expected_result_skips_calibration(tmp_path):
    repo, service, _ = make_service(tmp_path)
    result = service.ingest(
        Event(
            user_id="u1",
            source="task",
            type="commitment.slipped",
            facts={"domain": "work", "reschedule_count": 2},
            confidence=0.95,
        )
    )
    assert result and result.advice
    assert "未来三天内" in result.advice.adopted_expected_result
    legacy = result.advice.model_copy(
        update={"adopted_expected_result": None, "adopted_confidence": None}
    )
    repo.update_advice(legacy)
    repo.record_feedback("u1", legacy.id, FeedbackCreate(kind="adopted"))
    adopted = repo.get_advice("u1", legacy.id)
    assert adopted is not None and adopted.status == AdviceStatus.ADOPTED

    repo.record_outcome(
        "u1",
        adopted,
        OutcomeCreate(status=OutcomeStatus.INCORRECT, actual_result="结果未达成"),
    )

    trust = repo.trust_summary("u1", "work", adopted.effective_level)
    assert trust.judged == 0
    assert trust.brier == 1.0
    verified = repo.get_advice("u1", adopted.id)
    assert verified is not None and verified.status == AdviceStatus.VERIFIED
    repo.close()


def test_notification_problem_creates_goal_cited_advice(tmp_path):
    repo, service, goal = make_service(tmp_path)
    result = service.ingest(
        Event(
            user_id="u1",
            source="android.notification",
            type="notification.posted",
            facts={
                "package": "com.example.deploy",
                "title": "生产部署失败",
                "text": "连接超时，请检查发布任务",
            },
            confidence=0.98,
        )
    )

    assert result is not None
    assert result.decision == "publish"
    assert result.advice is not None
    assert result.advice.goal_quote == goal.quote
    assert "错误" in result.advice.first_step
    repo.close()


def test_strong_l3_safety_signal_breaks_quiet_hours_even_during_cold_start(tmp_path):
    repo, service, _ = make_service(tmp_path)
    result = service.ingest(
        Event(
            user_id="u1",
            source="android.notification",
            type="notification.posted",
            facts={"title": "可疑登录", "text": "发现未经授权的登录操作"},
            confidence=0.99,
        ),
        ContextSnapshot(quiet_hours=True),
    )

    assert result is not None and result.advice is not None
    assert result.effective_level == AdviceLevel.L2
    assert result.advice.delivery == "immediate"
    assert "EMERGENCY_TIMING_OVERRIDE" in result.reason_codes
    assert "CONTEXT_DEFERRED" not in result.reason_codes
    repo.close()


def test_expanded_notification_text_is_understood(tmp_path):
    repo, service, goal = make_service(tmp_path)
    result = service.ingest(
        Event(
            user_id="u1",
            source="android.notification",
            type="notification.posted",
            facts={
                "package": "com.example.deploy",
                "title": "发布状态",
                "text": "查看详情",
                "big_text": "生产发布任务同步失败，连接超时",
                "text_lines": ["请保留错误码", "不要重复提交"],
            },
            confidence=0.98,
        )
    )

    assert result is not None and result.advice is not None
    assert result.advice.goal_id == goal.id
    assert "同步失败" in result.advice.evidence[0].fact
    repo.close()


def test_local_speech_transcript_can_trigger_goal_cited_advice(tmp_path):
    repo, service, goal = make_service(tmp_path)
    result = service.ingest(
        Event(
            user_id="u1",
            source="android.microphone.transcript",
            type="speech.transcript",
            facts={
                "transcript": "AI替身生产部署失败，连接一直超时，需要处理",
                "language": "zh-CN",
                "context": "audio_transcript",
            },
            confidence=0.94,
            sensitivity="restricted",
            consent_scope="alpha.minimized_context",
        )
    )

    assert result is not None and result.advice is not None
    assert result.decision == "publish"
    assert result.advice.goal_id == goal.id
    assert result.advice.goal_quote == goal.quote
    assert "部署失败" in result.advice.evidence[0].fact
    repo.close()


def test_neutral_notification_is_recorded_without_advice(tmp_path):
    repo, service, _ = make_service(tmp_path)
    result = service.ingest(
        Event(
            user_id="u1",
            source="android.notification",
            type="notification.posted",
            facts={"package": "com.example.chat", "title": "项目周报", "text": "进度正常"},
        )
    )

    assert result is None
    assert repo.list_advice("u1") == []
    repo.close()


def test_problem_requires_goal_match_when_multiple_goals_exist(tmp_path):
    repo, service, _ = make_service(tmp_path)
    repo.insert_goal(
        Goal(
            user_id="u1",
            domain="health",
            title="改善睡眠",
            quote="这个季度要稳定睡眠",
        )
    )
    result = service.ingest(
        Event(
            user_id="u1",
            source="android.notification",
            type="notification.posted",
            facts={"package": "com.example.unknown", "title": "操作失败", "text": "连接超时"},
        )
    )

    assert result is None
    assert repo.list_advice("u1") == []
    repo.close()


def test_goal_keywords_route_problem_to_correct_domain(tmp_path):
    repo, service, _ = make_service(tmp_path)
    health = repo.insert_goal(
        Goal(
            user_id="u1",
            domain="health",
            title="改善睡眠",
            quote="这个季度要稳定睡眠",
            target={"keywords": ["睡眠监测"]},
        )
    )
    result = service.ingest(
        Event(
            user_id="u1",
            source="android.notification",
            type="notification.posted",
            facts={"package": "com.example.health", "title": "睡眠监测同步失败", "text": "连接超时"},
            confidence=0.96,
        )
    )

    assert result is not None and result.advice is not None
    assert result.advice.goal_id == health.id
    assert result.advice.domain == "health"
    repo.close()


def test_resolved_problem_does_not_interrupt(tmp_path):
    repo, service, _ = make_service(tmp_path)
    result = service.ingest(
        Event(
            user_id="u1",
            source="android.notification",
            type="notification.posted",
            facts={"title": "部署错误已解决", "text": "检查通过，服务恢复"},
        )
    )

    assert result is None
    assert repo.list_advice("u1") == []
    repo.close()


def test_security_problem_starts_at_l2_until_trust_is_earned(tmp_path):
    repo, service, goal = make_service(tmp_path)
    result = service.ingest(
        Event(
            user_id="u1",
            source="android.notification",
            type="notification.posted",
            facts={"title": "检测到可疑登录", "text": "该操作可能并非由你发起"},
            confidence=0.99,
        )
    )

    assert result is not None and result.advice is not None
    assert result.advice.requested_level == AdviceLevel.L3
    assert result.advice.effective_level == AdviceLevel.L2
    assert result.advice.goal_quote == goal.quote
    assert result.advice.alternative
    repo.close()


def test_raw_mobile_usage_is_aggregated_for_bound_goal_packages(tmp_path, monkeypatch):
    now = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)
    monkeypatch.setattr("app.service.utc_now", lambda: now)
    repo = Repository(tmp_path / "usage.db")
    goal = repo.insert_goal(
        Goal(
            user_id="u1",
            domain="work",
            title="完成AI替身开发",
            quote="本周投入十小时完成AI替身开发",
            target={"weekly_hours": 10, "packages": ["com.github.android"]},
        )
    )
    repo.set_charter("u1", "work", AdviceLevel.L3, False)
    result = ProactiveService(repo).ingest(
        Event(
            user_id="u1",
            source="android.usage",
            type="app.foreground_session",
            occurred_at=now,
            facts={
                "package": "com.github.android",
                "app_label": "GitHub",
                "duration_ms": 3_600_000,
                "timezone_offset_minutes": 480,
            },
            confidence=0.96,
        )
    )

    assert result is not None and result.advice is not None
    assert result.advice.goal_id == goal.id
    assert "1.0 小时" in result.advice.evidence[0].fact
    repo.close()


def test_weekly_usage_does_not_compare_monday_with_full_week_target(tmp_path, monkeypatch):
    now = datetime(2026, 7, 27, 4, tzinfo=timezone.utc)
    monkeypatch.setattr("app.service.utc_now", lambda: now)
    repo = Repository(tmp_path / "usage-monday.db")
    repo.insert_goal(
        Goal(
            user_id="u1",
            domain="work",
            title="完成AI替身开发",
            quote="本周投入十小时完成AI替身开发",
            target={"weekly_hours": 10, "packages": ["com.github.android"]},
        )
    )
    result = ProactiveService(repo).ingest(
        Event(
            user_id="u1",
            source="android.usage",
            type="app.foreground_session",
            occurred_at=now,
            facts={
                "package": "com.github.android",
                "app_label": "GitHub",
                "duration_ms": 3_600_000,
                "timezone_offset_minutes": 480,
            },
        )
    )

    assert result is None
    assert repo.list_advice("u1") == []
    repo.close()


def test_unbound_app_usage_does_not_create_false_drift(tmp_path):
    repo = Repository(tmp_path / "usage.db")
    repo.insert_goal(
        Goal(
            user_id="u1",
            domain="work",
            title="完成AI替身开发",
            quote="本周投入十小时完成AI替身开发",
            target={"weekly_hours": 10, "packages": ["com.github.android"]},
        )
    )
    result = ProactiveService(repo).ingest(
        Event(
            user_id="u1",
            source="android.usage",
            type="app.foreground_session",
            facts={"package": "com.example.video", "app_label": "Video", "duration_ms": 7_200_000},
        )
    )

    assert result is None
    assert repo.list_advice("u1") == []
    repo.close()


def test_windows_title_can_bind_usage_when_goal_has_no_package_map(tmp_path, monkeypatch):
    now = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)
    monkeypatch.setattr("app.service.utc_now", lambda: now)
    repo = Repository(tmp_path / "inferred-windows-usage.db")
    goal = repo.insert_goal(
        Goal(
            user_id="u1",
            domain="work",
            title="Finish My AI Twin alpha",
            quote="This week I will finish the My AI Twin alpha milestone",
            target={"weekly_hours": 10},
        )
    )
    repo.set_charter("u1", "work", AdviceLevel.L3, False)

    result = ProactiveService(repo).ingest(
        Event(
            user_id="u1",
            source="windows.foreground",
            type="app.foreground_session",
            occurred_at=now,
            facts={
                "package": "Code.exe",
                "app_label": "My AI Twin - Visual Studio Code",
                "window_title": "My AI Twin - Visual Studio Code",
                "duration_ms": 3_600_000,
                "timezone_offset_minutes": 480,
            },
            confidence=0.96,
        )
    )

    assert result is not None and result.advice is not None
    assert result.advice.goal_id == goal.id
    repo.close()
