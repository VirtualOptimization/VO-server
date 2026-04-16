# 🚀 VO-server

> 🏠 **공간 스캔 기반 가구 배치 최적화 백엔드 서버**  
> iOS LiDAR로 스캔한 공간 정보를 바탕으로 가구 배치를 자동 최적화하고,  
> VR 환경에서 실시간 인터랙션을 제공하는 서비스입니다.

---

## 👨‍💻 개발자 정보

| 역할 | 이름 | GitHub |
|------|------|--------|
| BE | 권예원 | [@e0ng](https://github.com/e0ng) |
| BE | 한정현 | [@JungHyunHann](https://github.com/JungHyunHann) |

---

## 📌 프로젝트 개요

- 📱 **iOS LiDAR 기반 공간 스캔**  
  공간 및 가구의 기하학적 데이터를 수집

- 🧠 **가구 배치 자동 최적화**  
  알고리즘을 통한 공간 활용도 극대화

- 🕶️ **VR 환경 동기화**  
  최적화된 결과를 VR에서 확인 및 직접 조작

- 🔄 **실시간 인터랙션**  
  WebSocket 기반 가구 이동 및 상태 즉각 반영

---

## 🛠️ 기술 스택

### 💻 Backend & Database
- Language: Python  
- Framework: FastAPI  
- Database: PostgreSQL  
- ORM: SQLAlchemy, Alembic  

### ☁️ Infrastructure (AWS)
- Compute: ECS (Docker), Lambda, Step Functions  
- Storage: S3  

### ⚙️ Processing & Realtime
- Algorithm: Shapely (기하학 기반 최적화 계산)  
- 3D Tool: Blender CLI (USDZ → FBX 변환)  
- Communication: FastAPI WebSocket  

---

## ✨ 주요 기능

1. 📥 **데이터 수집**  
   iOS에서 전송된 공간 스캔 JSON 데이터 수신 및 DB 저장  

2. 🧩 **배치 최적화**  
   Shapely 기반 바닥 점유율 계산 및 최적 가구 위치 산출  

3. 🔄 **포맷 변환**  
   USDZ → FBX 자동 변환 (VR 호환성 확보)  

4. 🔑 **VR 조회 시스템**  
   6자리 확인 코드 기반 공간 데이터 조회  

5. 🎮 **실시간 동기화**  
   WebSocket을 통한 가구 이동 상태 반영 및 버전 관리  

---

## 📁 폴더 구조

```bash
vo-server/
├── server/        # 🚀 FastAPI 메인 앱 (API, WebSocket)
├── workers/       # ⚙️ 최적화 알고리즘 & 변환 워커
├── infra/         # ☁️ AWS CDK (IaC)
└── shared/        # 📦 DB 스키마 & 공통 모델
````

---

## 🗄️ RDS 준비

- `infra/` 아래에 AWS CDK 기반 RDS 스택 골격이 있습니다.
- 기본값은 개발용 PostgreSQL 인스턴스입니다.
- 실제 배포 절차와 운영용 설정값은 내부 배포 문서에서 관리합니다.
- 현재 저장소 기준 내부 가이드는 [docs/internal/rds-deploy.md](/Users/kwon-yewon/Desktop/Duksung/2026/AJA/VO-server/docs/internal/rds-deploy.md:1)에 정리되어 있습니다.

---

## 🌿 협업 가이드

### 🔀 브랜치 전략

| 브랜치    | 설명                   |
| ------ | -------------------- |
| main   | 🚨 배포용 (직접 푸시 금지)    |
| dev    | 🔄 개발 통합 브랜치 (PR 필수) |
| feat/* | ✨ 기능 개발              |
| fix/*  | 🐛 버그 수정             |
| refactor/*  | ♻️ 코드 개선             |

---

### 📝 커밋 컨벤션

* `feat` : 새로운 기능 추가
* `fix` : 버그 수정
* `refactor` : 코드 리팩토링
* `docs` : 문서 수정
* `chore` : 설정 및 기타 작업
* `test` : 테스트 코드

#### 📌 예시

```bash
feat: 스캔 JSON 수신 Lambda 추가
fix: 확인코드 중복 생성 버그 수정
```

---
