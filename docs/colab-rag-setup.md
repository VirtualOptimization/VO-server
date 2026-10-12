# Neufert 규칙 추출·임베딩 가이드

현재 저장소 코드 기준입니다. 파일명은 기존 링크 호환을 위해 유지했습니다.
이전 Colab/Qwen 셀은 별도 실험 코드이며 현행 서버 추출 파이프라인이 아닙니다.
Colab/GPU는 필수가 아닙니다. 실제 배포 환경의 모델·DB 적재 상태는 별도 확인합니다.

## 두 경로 구분

| 경로 | 처리 | 결과 |
| --- | --- | --- |
| 시각 규칙 추출 | 페이지 이미지 + 선택적 OCR → OpenAI Vision → 표준화·검증 | 후보 규칙 JSONL + review JSONL |
| 검색용 문서 적재 | 이미 청킹된 텍스트 → 로컬 BGE-M3 임베딩 | PostgreSQL `neufert_chunks` / pgvector |

시각 추출 자체가 청킹·임베딩을 수행하지는 않습니다. `ingest_neufert_chunks.py`도
준비된 청크를 적재할 뿐 문서를 청킹하지 않습니다. 승인 규칙은 최적화기에서
`NEUFERT_RULES_JSONL`로 읽는 별도 경로입니다. 후보 규칙을 문서 청크 테이블에
그대로 넣으면 안 됩니다.

## 준비

저장소 루트에서 전용 가상환경을 사용합니다.

```bash
python3 -m venv .venv-rag
source .venv-rag/bin/activate
python -m pip install -r requirements.txt -r workers/rag/requirements.txt pillow
```

로컬 `.env` 또는 실행 환경에 필요한 값을 설정하며 키·비밀번호는 커밋하지 않습니다.

- 시각 추출: `OPENAI_API_KEY`, 선택적 `OPENAI_VISION_MODEL` (코드 기본값 `gpt-4.1-mini`)
- DB 적재: `DATABASE_URL`, `ASYNC_DATABASE_URL` 및 적용된 DB 마이그레이션
- 최적화 보조 Claude의 `ANTHROPIC_API_KEY`는 추출용 OpenAI 키와 별개입니다.

`.env`는 dotenv로 읽습니다. 셸 스크립트 문법이 아니므로 무조건 `source .env`로
불러오지 않습니다. BGE-M3는 첫 사용 시 모델 다운로드와 로컬 메모리가 필요합니다.

## 시각 추출 입력

입력 JSONL은 이미지당 한 행이며 같은 페이지의 행은 한 요청으로 묶입니다.

```json
{"page_number":255,"image_path":"/data/neufert/pages/page-255.png","ocr_numbers":[]}
```

OCR JSONL은 선택 사항이며 다음 형식입니다 (`content`도 지원).

```json
{"page_number":255,"text":"해당 페이지의 OCR 텍스트"}
```

PDF 순서와 인쇄 쪽수가 다른 경우 직접 대응을 확인해야 합니다. 추출기는 PDF를
렌더링하거나 누락 페이지를 생성하지 않습니다. `--image-root`는 저장된 경로를
찾지 못할 때 파일명으로 이미지를 찾는 경로입니다.

## 한 페이지 테스트

`DATA`를 실제 폴더로 바꾸고 입력에는 테스트할 페이지 한 장만 넣습니다.
출력은 종료 시 덮어쓰므로 원본 입력과 다른 경로를 사용합니다.

```bash
DATA=/data/neufert
python -u -m workers.rag.extract_visual_rules_openai_page \
  "$DATA/one_page.jsonl" "$DATA/candidates_test.jsonl" \
  --review-jsonl "$DATA/review_test.jsonl" \
  --image-root "$DATA/pages" \
  --pages-jsonl "$DATA/pages_ocr.jsonl" \
  --max-images 1 --max-side 1600 --limit-pages 1 --residential-only
```

이 명령은 OpenAI API를 호출합니다. `--residential-only`는 코드의 `RESIDENTIAL_PAGES`
범위(현재 232~265, 입력의 페이지 번호 기준)로 제한하며 입력에 없는 페이지를 추가하지 않습니다.
OCR 텍스트는 공백 정리 후 페이지별 최대 7000자까지만 전달됩니다. 전체 실행은 검증한 입력
파일로 교체하고 `--limit-pages`를 제거합니다.

`--max-side 1600`은 긴 변을 최대 1600픽셀로 줄입니다. 원본 파일은 변경하지 않지만
작은 도면 치수는 읽기 어려워질 수 있어 원본과 대조해야 합니다. `--max-images`는
페이지별 이미지 수 제한이지 PDF 페이지 수가 아닙니다.

요청별 timeout은 60초, 재시도는 0회이며 전체 실행 시간 상한은 아닙니다.
진행 로그는 즉시 출력하지만 JSONL은 전체 루프 종료 후 저장합니다. 실행 도중
출력 파일의 행 수가 증가하지 않아도 멈췄다고 단정할 수 없습니다.

## 검수와 승인

후보의 `parsed_rule`에는 `subject`, `object`, `relation`, `type`, `direction`, 거리·권장값,
근거 등이 저장됩니다. 수치 없는 관계는 qualitative 규칙으로 남길 수 있습니다.
최소값과 권장값을 구분하고 confidence만으로 승인하지 않습니다.

```bash
python -m workers.rag.filter_layout_rules \
  "$DATA/candidates_test.jsonl" "$DATA/layout_candidates.jsonl"
```

이 필터는 허용된 type·relation·subject·object 목록을 검사합니다. 효율성 근거 판별이나
보류 세트 저장을 자동 보장하지 않으며 제외된 행은 출력에 남기지 않습니다.
원래 후보와 review를 보존하고 다음을 별도로 확인합니다.

- 가구 자체 치수를 통로 거리로 오해하지 않았는지
- 주거용인지, 적용 조건과 방향이 맞는지
- 속담·단순 설명·배치 예시를 의무 규칙으로 오해하지 않았는지
- 단위, 최소/권장 구분, 중복과 상충 조건이 맞는지

`extract_layout_rules_generic.py`는 별도 일반화 추출 CLI입니다. 페이지 추출기와
표준화 사전·검증 로직이 같지 않아 동일한 결과를 보장하지 않습니다.

## 검색용 문서 청크 DB 적재

청크 JSONL 예시:

```json
{"source_name":"Neufert","page_start":255,"page_end":255,"section_title":"Bedroom","content":"검색할 원문 텍스트"}
```

DB 연결과 마이그레이션을 준비한 뒤 실행합니다.

```bash
python -m workers.rag.ingest_neufert_chunks "$DATA/neufert_chunks.jsonl" --batch-size 32
```

BGE-M3의 정규화된 1024차원 벡터와 pgvector HNSW cosine 인덱스를 사용합니다.
기존 DB에 같은 content hash가 있으면 건너뜁니다. 입력 파일 내부 중복은 사전에
제거해야 합니다. 임베딩은 로컬 연산이며 OpenAI 임베딩 API를 호출하지 않습니다.

`select_extraction_pages.py`는 별도 OpenAI 임베딩 기반 페이지 탐색 도구입니다.
DB 적재의 필수 단계가 아니며 실행하면 API 사용량이 발생합니다.

## 최적화기에 승인 규칙 연결

```bash
export NEUFERT_RULES_JSONL=/data/neufert/layout_approved.jsonl
```

최적화 문제 생성 시 승인 규칙을 읽어 매칭합니다. 서버에서 접근 가능한 경로를 쓰고
개인 Mac 절대 경로를 배포 설정에 사용하지 않습니다. 규칙 JSONL 연결과 문서 청크
pgvector 검색은 서로 다른 기능입니다.
