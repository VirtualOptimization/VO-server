# RDS Deploy Guide

이 문서는 저장소의 개발용 RDS CDK 스택과 Alembic 적용 절차입니다.
운영 중인 RDS의 실제 설정을 증명하는 문서는 아닙니다. 기존 DB에 적용하기 전
대상 계정·리전·스택과 백업을 확인합니다.

## 준비

`infra/` 아래에 AWS CDK 기반 RDS 스택 골격이 있습니다.
기본값은 개발용 PostgreSQL 인스턴스입니다.

로컬에서 필요한 패키지 설치:

```bash
python3 -m venv .venv-infra
source .venv-infra/bin/activate
python -m pip install -r infra/requirements.txt
```

## 배포 전 환경 변수

예시:

```bash
export CDK_DEFAULT_ACCOUNT=123456789012
export CDK_DEFAULT_REGION=ap-southeast-2
export VO_RDS_STACK_NAME=vo-rds-stack
export VO_RDS_INSTANCE_IDENTIFIER=vo-rds-dev
export VO_RDS_DB_NAME=vo_db
export VO_RDS_DB_USERNAME=vo_admin
export VO_RDS_PUBLICLY_ACCESSIBLE=true
export VO_RDS_ALLOWED_IPV4="YOUR_PUBLIC_IP/32"
export VO_RDS_ALLOCATED_STORAGE_GB=20
export VO_RDS_MAX_ALLOCATED_STORAGE_GB=20
```

주의:

- 개발 단계에서 로컬에서 바로 Alembic을 붙이려면 `VO_RDS_PUBLICLY_ACCESSIBLE=true`와
  접속을 허용할 IP CIDR(`VO_RDS_ALLOWED_IPV4`)이 필요합니다.
- `YOUR_PUBLIC_IP/32`는 실제 접속할 공인 IP CIDR로 교체해야 합니다.
  코드 기본값은 `0.0.0.0/0`이므로 반드시 명시적으로 제한합니다.
- 공개 접근 모드는 public subnet과 NAT Gateway 0개, 비공개 접근 모드는 private
  subnet과 NAT Gateway 1개를 만듭니다. 공개 접근을 운영 환경의 기본 권장으로
  해석하면 안 됩니다. 비공개 모드에서는 DB 접속 네트워크 경로도 준비합니다.
- 테스트용 스토리지 상한은 `VO_RDS_MAX_ALLOCATED_STORAGE_GB=20`처럼 기본값과 동일하게 두는 편이 단순합니다.

## 스택 배포

```bash
cdk --version
cdk synth --app "python -m infra.app"
cdk diff --app "python -m infra.app"
cdk deploy --app "python -m infra.app"
```

AWS 인증정보와 별도 AWS CDK CLI가 필요합니다. `infra/requirements.txt`는 Python
라이브러리만 설치하므로 `.venv/bin/cdk`는 생성되지 않습니다. 처음 사용하는
계정·리전은 팀의 CDK bootstrap 절차도 확인합니다.
위 명령은 저장소 루트에서 실행하며 루트 `cdk.json`의 `python3 infra/app.py` 대신
`infra` 패키지를 찾을 수 있는 모듈 실행 경로를 명시합니다.

배포 후 endpoint, port, secret ARN으로 접속 정보를 확인합니다. 비밀번호는
Secrets Manager에서 안전하게 확인하고 문서·로그·Git에 남기지 않습니다.

## 후속 작업

1. RDS endpoint와 인증정보 확인
2. 서버 가상환경에 `requirements.txt` 설치 후 `.env`의 `DATABASE_URL`과 `ASYNC_DATABASE_URL` 설정
3. 저장소 루트에서 `python -m alembic upgrade head`, `python -m alembic current` 실행
4. 보안 그룹과 허용 IP 범위 다시 점검

## 운영 메모

- 동기 URL은 `postgresql+psycopg://...`, 비동기 URL은 `postgresql+asyncpg://...` 형식입니다.
  비밀번호의 URL 특수문자는 인코딩하고 `.env`를 무조건 셸에서 source하지 않습니다.
- pgvector 마이그레이션은 `CREATE EXTENSION IF NOT EXISTS vector`를 실행하므로
  대상 DB의 확장 지원과 실행 계정 권한을 확인해야 합니다.
- CDK 코드는 PostgreSQL 16.3, t4g.micro, Single-AZ, 백업 보존 1일을 지정합니다.
  삭제 보호가 꺼져 있고 `RemovalPolicy.DESTROY` 및 자동 백업 삭제가 설정돼 있습니다.
  운영용 보호 정책으로 간주하지 말고 배포 가능 버전과 설정은 별도로 검토합니다.
- 현재 GitHub 배포 워크플로는 의존성 설치와 서버 재시작을 수행하지만 Alembic은
  자동 실행하지 않습니다. 필요한 마이그레이션은 배포 절차에 별도로 포함합니다.
- 이 문서 점검에서는 AWS 배포·DB 마이그레이션을 실행하지 않았습니다.
