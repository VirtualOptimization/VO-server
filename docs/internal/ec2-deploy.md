# EC2 Deploy Guide

운영 서버(EC2 + RDS + S3)를 새 AWS 계정에 처음부터 세팅하는 순서입니다.
2026-11 서울 리전으로 다시 세팅할 때 실제로 한 절차를 기준으로 정리했습니다.
저장소에 없는 설정(콘솔 리소스, systemd, nginx, `.env`)은 이 문서가 기준입니다.

## 1. AWS 리소스 (콘솔, 서울 `ap-northeast-2`)

| 리소스 | 설정 |
|---|---|
| S3 | 범용 버킷 `vo-seoul-s3`, 글로벌 네임스페이스, 퍼블릭 액세스 차단, ACL 비활성화 |
| IAM 역할 | `vo-ec2-s3-role` (신뢰 엔터티 EC2), 아래 인라인 정책만 연결 |
| 보안 그룹 | `vo-server-sg`: 22·80·443 `0.0.0.0/0` (GitHub Actions가 SSH로 배포) |
| | `vo-rds-sg`: 5432를 `vo-server-sg`에서만 허용 |
| EC2 | Ubuntu Server 26.04 LTS x86, t3.small, gp3 20GiB, 키 페어 `vo-server-key`, IAM 인스턴스 프로파일 `vo-ec2-s3-role`, 탄력적 IP 연결 |
| RDS | PostgreSQL 16, db.t4g.micro, gp3 20GiB, 퍼블릭 액세스 아니요, `vo-rds-sg`만, 초기 DB 이름 `vo_db`, 자동 백업 7일, 삭제 방지 |

IAM 인라인 정책:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": "arn:aws:s3:::vo-seoul-s3"},
    {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], "Resource": "arn:aws:s3:::vo-seoul-s3/*"}
  ]
}
```

- 서버는 IAM 역할로 S3에 접근하므로 서버 `.env`에 AWS 키를 넣지 않습니다.
- RDS 초기 DB 이름을 비우면 `vo_db`가 만들어지지 않습니다. 이때는 3단계의 생성 스크립트를 씁니다.

## 2. 서버 기본 설치

```bash
ssh -i ~/.ssh/vo-server-key.pem ubuntu@<EC2_IP>
sudo apt update && sudo apt upgrade -y && sudo apt install -y git python3-venv python3-pip nginx unzip curl
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile && echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
cd ~ && git clone https://github.com/VirtualOptimization/VO-server.git && cd VO-server && git checkout dev
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
```

Ubuntu 26.04의 기본 Python은 3.14입니다. 스왑은 3D 변환·최적화 중 메모리 부족으로 프로세스가 죽는 것을 막습니다.

## 3. `.env`와 DB

`.env.example`을 기준으로 `~/VO-server/.env`를 만들고 `chmod 600 .env` 합니다.
`DATABASE_URL`, `ASYNC_DATABASE_URL`은 RDS 엔드포인트를 가리키게 합니다.
JWT 값 3개는 배포 때 GitHub 시크릿으로 덮어씁니다.

`vo_db`가 없다면 먼저 만듭니다.

```bash
python - <<'EOF'
import psycopg
from dotenv import dotenv_values
url = dotenv_values(".env")["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
with psycopg.connect(url.rsplit("/", 1)[0] + "/postgres", autocommit=True) as conn:
    conn.execute("CREATE DATABASE vo_db")
EOF
```

```bash
alembic upgrade head
bash scripts/setup_gltf2usdz.sh
```

## 4. 서비스와 nginx

`/etc/systemd/system/vo-server.service`:

```ini
[Unit]
Description=VO API Server
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/VO-server
Environment=ENV=local
Environment=PATH=/home/ubuntu/.bun/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=/home/ubuntu/VO-server/.venv/bin/uvicorn server.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now vo-server
bash scripts/install_conversion_worker_service.sh
```

`/etc/nginx/sites-available/vo-server`:

```nginx
server {
    listen 80 default_server;
    server_name _;
    client_max_body_size 100M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 600s;
    }
}
```

```bash
sudo rm -f /etc/nginx/sites-enabled/default && sudo ln -sf /etc/nginx/sites-available/vo-server /etc/nginx/sites-enabled/ && sudo nginx -t && sudo systemctl reload nginx
```

`deploy.yml`의 헬스체크가 80번(`http://127.0.0.1/health`)을 확인하므로 nginx가 필요합니다.

## 5. 기본 가구 카탈로그

RoomPlan 카탈로그 번들(`RoomPlanCatalog.bundle`, USDC 42개)이 원본입니다.
S3 `asset/`은 번들의 `Resources/` 구조를 그대로 따릅니다.

```bash
# Mac에서
scp -i ~/.ssh/vo-server-key.pem -r RoomPlanCatalog.bundle ubuntu@<EC2_IP>:~/
```

서버에서 (`~/VO-server`, venv 활성화):

```bash
# 1) USDC → GLB 변환만 (결과: tmp/roomplan-catalog/glb/)
python scripts/prepare_roomplan_catalog.py --catalog-root ~/RoomPlanCatalog.bundle --skip-upload --skip-db

# 2) S3 asset/ 업로드
python - <<'EOF'
from pathlib import Path
import boto3
s3 = boto3.client("s3", region_name="ap-northeast-2")
root = Path("tmp/roomplan-catalog/glb")
for p in sorted(root.rglob("*.rooms.glb")):
    s3.upload_file(str(p), "vo-seoul-s3", f"asset/{p.relative_to(root).as_posix()}", ExtraArgs={"ContentType": "model/gltf-binary"})
EOF

# 3) DB 등록 → 4) 색상·USDZ 생성 (먼저 dry-run) → 5) 치수
python scripts/seed_base_furniture_models_from_s3.py
python scripts/recolor_base_furniture_assets.py
python scripts/recolor_base_furniture_assets.py --apply
python scripts/update_furniture_dimensions_from_catalog.py --catalog-root ~/RoomPlanCatalog.bundle
```

- 1단계 결과가 `Items=42, Failed=0`이어야 합니다. starLegs 의자 7종은 기본 변환이 실패하고 UV를 뺀 재변환으로 처리됩니다.
- 2단계 업로드는 `.glb`만 올립니다. `.usdz`는 4단계가 만듭니다.

## 6. GitHub Actions 배포

저장소 Actions 시크릿 `EC2_HOST`(탄력적 IP), `EC2_USER`(`ubuntu`), `EC2_SSH_KEY`(`.pem` 내용), `JWT_SECRET_KEY`, `JWT_ALGORITHM`, `ACCESS_TOKEN_EXPIRE_MINUTES`를 맞춥니다.

```bash
gh secret set EC2_HOST --body "<EC2_IP>" --repo VirtualOptimization/VO-server
gh secret set EC2_SSH_KEY --repo VirtualOptimization/VO-server < ~/.ssh/vo-server-key.pem
```

이후 `dev`에 푸시하면 `git pull` → `pip install` → `setup_gltf2usdz.sh` → `alembic upgrade head` → `vo-server` 재시작이 자동으로 실행됩니다.

## 7. 확인

```bash
curl http://<EC2_IP>/health
.venv/bin/python -c "import boto3; boto3.client('s3', region_name='ap-northeast-2').put_object(Bucket='vo-seoul-s3', Key='healthcheck.txt', Body=b'ok'); print('S3 OK')"
sudo reboot   # 재부팅 후 /health가 다시 응답하는지 확인
```

- Swagger: `http://<EC2_IP>/docs`
- `GET /api/rooms/catalog/models`가 42개를 돌려주고, `glb_url`·`usdz_url`이 열려야 합니다.
- 로그: `sudo journalctl -u vo-server -n 50 --no-pager`
