# RDS Deploy Guide

이 문서는 개발용 RDS 생성과 Alembic 적용 절차를 정리한 내부용 메모입니다.

## 준비

`infra/` 아래에 AWS CDK 기반 RDS 스택 골격이 있습니다.
기본값은 개발용 PostgreSQL 인스턴스입니다.

로컬에서 필요한 패키지 설치:

```bash
python3 -m venv .venv
.venv/bin/pip install -r infra/requirements.txt
```

## 배포 전 환경 변수

예시:

```bash
export CDK_DEFAULT_ACCOUNT=123456789012
export CDK_DEFAULT_REGION=ap-northeast-2
export VO_RDS_STACK_NAME=vo-rds-stack
export VO_RDS_INSTANCE_IDENTIFIER=vo-rds-dev
export VO_RDS_DB_NAME=vo_db
export VO_RDS_DB_USERNAME=vo_admin
export VO_RDS_PUBLICLY_ACCESSIBLE=true
export VO_RDS_ALLOWED_IPV4=0.0.0.0/0
```

주의:

- 개발 단계에서 로컬에서 바로 Alembic을 붙이려면 `VO_RDS_PUBLICLY_ACCESSIBLE=true`와
  접속을 허용할 IP CIDR(`VO_RDS_ALLOWED_IPV4`)이 필요합니다.
- 예시는 `0.0.0.0/0`로 열어두었지만, 실제로는 본인 공인 IP/32로 좁히는 것을 권장합니다.

## 스택 배포

```bash
.venv/bin/cdk deploy
```

배포 후 출력되는 endpoint, port, secret ARN을 이용해 `DATABASE_URL`을 맞추고 Alembic을 실행합니다.

## 후속 작업

1. RDS endpoint와 인증정보 확인
2. `DATABASE_URL` 업데이트
3. `alembic upgrade head` 실행
4. 보안 그룹과 허용 IP 범위 다시 점검

## 운영 메모

- 공개 저장소로 유지할 경우 이 문서도 장기적으로는 별도 비공개 문서로 이동하는 편이 안전합니다.
