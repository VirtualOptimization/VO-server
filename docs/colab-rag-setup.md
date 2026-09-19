# V-O RAG Colab Setup

이 문서는 Neufert 텍스트 규칙과 도면·표 시각 규칙을 Colab GPU에서 처리하기 위한 실행 순서다.
로컬 맥북에서는 OCR 결과 정리와 데이터 검증만 하고, Qwen2.5-VL 추론은 Colab에서 실행한다.

## 1. Colab 런타임

Colab에서 `런타임 > 런타임 유형 변경 > T4 GPU`를 선택한다.

첫 번째 셀:

```python
from google.colab import drive
drive.mount('/content/drive')
```

두 번째 셀:

```bash
!pip install -q -U transformers accelerate bitsandbytes qwen-vl-utils pillow tqdm
```

세 번째 셀에서 GPU를 확인한다.

```python
import torch

assert torch.cuda.is_available(), 'Colab 런타임을 GPU로 변경하세요.'
print(torch.cuda.get_device_name(0))
print(f'CUDA memory: {torch.cuda.get_device_properties(0).total_memory / 1024**3:.1f} GB')
```

## 2. Drive 경로 확인

파일은 다음 구조로 둔다.

```text
MyDrive/V-O/opt_RAG/
  visual_regions_valid/visual_regions.jsonl
  visual_regions_valid/page-320.png
  visual_regions_valid/page-320.txt
```

```python
from pathlib import Path

BASE = Path('/content/drive/MyDrive/V-O/opt_RAG')
INPUT_JSONL = BASE / 'visual_regions_valid/visual_regions.jsonl'
IMAGE_ROOT = BASE / 'visual_regions_valid'
CANDIDATES = BASE / 'visual_rule_candidates_colab.jsonl'
REVIEW = BASE / 'visual_rule_review_colab.jsonl'

print('입력 존재:', INPUT_JSONL.exists(), INPUT_JSONL)
print('이미지 수:', len(list(IMAGE_ROOT.glob('*.png'))))
```

`visual_regions.jsonl` 안의 `image_path`가 맥북 경로여도 파일명만 사용해 Drive의 실제 이미지와 연결한다.

## 3. Qwen2.5-VL 3B 로드

4비트 양자화를 사용해 T4 16GB에서 실행한다. 이미지 하나씩 처리하므로 GPU 메모리 사용량이 안정적이다.

```python
import torch
from transformers import BitsAndBytesConfig, Qwen2_5_VLForConditionalGeneration, AutoProcessor

MODEL_NAME = 'Qwen/Qwen2.5-VL-3B-Instruct'
quantization_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_quant_type='nf4',
    bnb_4bit_compute_dtype=torch.float16,
    bnb_4bit_use_double_quant=True,
)

model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
    MODEL_NAME,
    quantization_config=quantization_config,
    device_map='auto',
)
processor = AutoProcessor.from_pretrained(
    MODEL_NAME,
    min_pixels=256 * 256,
    max_pixels=768 * 768,
)
```

## 4. 시각 규칙 추출 실행

아래 셀은 규칙 배열을 요구하고, 불확실한 결과는 자동 승인하지 않고 review 파일에 저장한다.

```python
import json
import re
from pathlib import Path
from PIL import Image
from tqdm.auto import tqdm
from qwen_vl_utils import process_vision_info

def resolve_image(record):
    source = Path(record.get('image_path', ''))
    candidates = [IMAGE_ROOT / source.name, IMAGE_ROOT / f"page-{record.get('page_number')}.png"]
    for path in candidates:
        if path.exists():
            return path
    return None

def parse_json(text):
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.I)
    match = re.search(r'\{.*\}|\[.*\]', text, flags=re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None

def extract_rules(image_path):
    prompt = '''Analyze this architectural furniture diagram or table.
Return JSON only in this exact shape:
{"rules":[{"target":"table|chair|bed|desk|sofa|wardrobe|window|door|room|other",
"type":"clearance|dimension|placement|accessibility",
"direction":"front|back|side|around|none",
"min_distance_m":null,"max_distance_m":null,
"confidence":0.0,"evidence":"short description"}]}

Rules:
- Extract multiple independent rules when multiple measurements are visible.
- Convert centimeters to meters and preserve visible numeric measurements.
- Do not invent a value that is not visible.
- If the diagram is not reliable, return {"rules":[]}.
- Use confidence below 0.85 when text or measurement is ambiguous.'''

    messages = [{
        'role': 'user',
        'content': [
            {'type': 'image', 'image': str(image_path)},
            {'type': 'text', 'text': prompt},
        ],
    }]
    text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    image_inputs, video_inputs = process_vision_info(messages)
    inputs = processor(
        text=[text],
        images=image_inputs,
        videos=video_inputs,
        padding=True,
        return_tensors='pt',
    ).to('cuda')
    with torch.inference_mode():
        generated = model.generate(**inputs, max_new_tokens=384, do_sample=False)
    generated = generated[:, inputs.input_ids.shape[1]:]
    answer = processor.batch_decode(generated, skip_special_tokens=True)[0]
    return parse_json(answer)

records = [json.loads(line) for line in INPUT_JSONL.read_text(encoding='utf-8').splitlines() if line.strip()]
accepted = []
review = []

for index, record in enumerate(tqdm(records, desc='시각 규칙 추출'), start=1):
    image_path = resolve_image(record)
    base = {'page_number': record.get('page_number'), 'image_path': str(image_path or '')}
    if image_path is None:
        review.append({**base, 'reason': 'image not found'})
        continue
    try:
        result = extract_rules(image_path) or {'rules': []}
        rules = result.get('rules', []) if isinstance(result, dict) else []
        valid = [r for r in rules if isinstance(r, dict) and float(r.get('confidence', 0)) >= 0.85]
        if valid:
            for rule in valid:
                accepted.append({**base, 'parsed_rule': rule})
        else:
            review.append({**base, 'raw_result': result, 'reason': 'no reliable visual rule'})
    except Exception as exc:
        review.append({**base, 'reason': f'{type(exc).__name__}: {exc}'})
    if index % 5 == 0:
        print(f'{index}/{len(records)} accepted={len(accepted)} review={len(review)}')

CANDIDATES.write_text('\n'.join(json.dumps(row, ensure_ascii=False) for row in accepted) + ('\n' if accepted else ''), encoding='utf-8')
REVIEW.write_text('\n'.join(json.dumps(row, ensure_ascii=False) for row in review) + ('\n' if review else ''), encoding='utf-8')
print('완료:', len(accepted), 'accepted /', len(review), 'review')
print(CANDIDATES)
print(REVIEW)
```

## 5. 결과 확인

```python
import json
from collections import Counter

candidate_rows = [json.loads(line) for line in CANDIDATES.read_text(encoding='utf-8').splitlines() if line.strip()]
review_rows = [json.loads(line) for line in REVIEW.read_text(encoding='utf-8').splitlines() if line.strip()]

print('승인 규칙:', len(candidate_rows))
print('검수 대상:', len(review_rows))
print('검수 사유:', Counter(row.get('reason') for row in review_rows))
for row in candidate_rows[:10]:
    print(row)
```

## 6. RAG DB 반영 전 검증

자동 승인 결과도 바로 DB에 넣지 말고 다음을 확인한다.

1. `target`이 실제 가구명인지 확인한다.
2. `min_distance_m`과 `max_distance_m`이 0보다 큰지 확인한다.
3. `min_distance_m <= max_distance_m`인지 확인한다.
4. 같은 페이지·같은 target·같은 type·같은 방향의 중복 규칙을 제거한다.
5. 검증한 JSONL만 BGE-M3 임베딩을 생성해 pgvector에 저장한다.

## 7. Colab CLI로 자동화할 때

브라우저에서 위 셀을 먼저 검증한 뒤, 추출 셀을 별도 `.py` 파일로 저장하면 공식 Colab CLI로 반복 실행할 수 있다.

```bash
uv tool install google-colab-cli
colab auth login
colab new -s vo-rag --gpu T4
colab exec -s vo-rag -f ./scripts/colab_rag_visual_extract.py
colab stop -s vo-rag
```

CLI의 `colab exec`는 로컬 파일을 원격 Colab 커널에서 실행한다. 결과 파일은 Drive에 저장하거나, CLI의 파일 다운로드 기능으로 회수한다. 현재 저장소에는 이 실행 파일을 아직 추가하지 않았으므로, 우선 브라우저 셀 방식으로 경로와 모델을 검증한다.
