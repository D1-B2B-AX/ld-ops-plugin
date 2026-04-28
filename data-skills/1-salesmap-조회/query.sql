-- 스킬 1: 세일즈맵 조회 (상태 파라미터화 — SQL/Won 분기)
-- 담당자의 딜 + 고객사 + 고객사 담당자 결합
-- settings.owner.name을 LIKE 파라미터로 치환하여 다중 LD 지원
-- status_scope 파라미터: 'SQL' (딜 플러그인) / 'Won' (운영 플러그인) / 'Both' (통합)
-- v0.2 (2026-04-22): 수강시작일/수강종료일 컬럼 추가 + Won 진행 중 교육 필터 활성화
-- v0.3 (2026-04-23): 코스 ID 컬럼 추가 — 드라이브 파일 매칭용 공통 식별자
-- v0.6 (2026-04-27): Won 모드 시 settings.data_sources.ops_included_course_formats 화이트리스트 적용
--                   파트장 미팅 결과: 온라인·구독·컨설팅·콘텐츠 제작 등은 9개 체크포인트 매칭 어려움 → 제외
--                   기본 화이트리스트: '출강', '복합(출강+온라인)'

-- ============================================
-- 메인 쿼리: SQL 단계 딜 + 고객사 + 담당자
-- ============================================
SELECT
  d.id AS deal_id,
  d."이름" AS deal_name,
  d."파이프라인 단계" AS stage_raw,         -- JSON 문자열, 후처리로 name 추출
  d."성사 가능성" AS win_probability_raw,   -- '["낮음"]' 형태
  d."예상 체결액" AS expected_amount,
  d."금액" AS amount,
  d."수주 예정일" AS expected_close_date,
  d."제안서 마감일" AS proposal_deadline,
  d."마감일" AS deadline,
  d."상태" AS status,
  d."과정포맷" AS course_format,
  d."교육 주제" AS course_topic,
  d."예상 교육 인원" AS expected_learners,
  d."예상 교육 일정" AS expected_schedule,
  d."기업 니즈" AS customer_needs,
  d."상담 문의 내용" AS inquiry,
  d."기획시트 링크" AS planning_sheet_link,
  d."최근 노트 작성일" AS last_note_date,
  d."최근 파이프라인 단계 수정 날짜" AS last_stage_change,
  d."수강시작일" AS edu_start,               -- Won 전용 (SQL 단계에서는 보통 NULL)
  d."수강종료일" AS edu_end,                 -- Won 전용
  d."코스 ID" AS course_id,                  -- 6자리 고유 ID (드라이브 파일명 매칭용)
  d.organizationId,
  o."이름" AS organization_name,
  o."업종" AS industry,
  o."기업 규모" AS company_size,
  o."성사된 딜 개수" AS past_won_deals,
  o."총 매출" AS total_revenue,
  o."최근 딜 성사 날짜" AS last_won_date,
  d.peopleId,
  p."이름" AS contact_name,
  p."이메일" AS contact_email,
  p."직급/직책" AS contact_title,
  p."담당 업무" AS contact_role
FROM deal d
LEFT JOIN organization o ON d.organizationId = o.id
LEFT JOIN people p ON d.peopleId = p.id
WHERE d."담당자" LIKE '%{owner_name}%'                                        -- settings.owner.name 치환 (다중 LD 지원)
  AND d."상태" = '{status_scope}'                                               -- status_scope: 'SQL' (딜) / 'Won' (운영)
  -- Won 모드에서만 진행 중인 교육만 (종료일이 미래이거나 미확정)
  -- SQL 모드에서는 수강종료일 상관없이 전부 통과 (앞의 'SQL' != 'Won'이 항상 참)
  AND ('{status_scope}' != 'Won' OR d."수강종료일" IS NULL OR d."수강종료일" >= date('now'))
  -- v0.6 (260427): Won 모드에서만 운영 체크포인트 적용 가능 과정포맷 화이트리스트
  -- {ops_format_list} 는 settings.data_sources.ops_included_course_formats 치환 (예: "'출강','복합(출강+온라인)'")
  AND ('{status_scope}' != 'Won' OR d."과정포맷" IN ({ops_format_list}))
ORDER BY d."최근 파이프라인 단계 수정 날짜" DESC;


-- ============================================
-- 보조 쿼리: 각 딜의 최근 메모 N건
-- ============================================
-- :deal_ids 는 메인 쿼리 결과의 deal_id 목록으로 치환
SELECT
  m.dealId,
  m.createdAt,
  m."유형" AS memo_type,
  substr(m.text, 1, 500) AS text_preview
FROM memo m
WHERE m.dealId IN (:deal_ids)
ORDER BY m.dealId, m.createdAt DESC;
-- 애플리케이션 레벨에서 dealId별 최근 3건으로 slice
