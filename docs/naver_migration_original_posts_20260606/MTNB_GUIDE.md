# 원문 기반 Markdown → 네이버 블로그 업로드 가이드

생성일: 2026-06-06

## 1. 결과물 위치

원문 기반 Markdown 파일은 이 폴더에 있습니다.

- `dify-practice/docs/naver_migration_original_posts_20260606/`

포함 글 수는 29개입니다.

제외한 글은 티스토리 운영/수익화 계열 4개입니다.

- `[티스토리운영] 네이버서치어드바이저#1`
- `[티스토리운영] 네이버서치어드바이저#2`
- `[티스토리운영] 구글서치콘솔#1`
- `[티스토리 애드센스 신청하기]`

전체 목록은 `_manifest.json`에서 확인할 수 있습니다.

## 2. Markdown 파일 구조

각 게시글 파일은 아래 구조입니다.

```md
---
source_url: 원문 티스토리 URL
original_title: 기존 티스토리 제목
suggested_titles:
  - 네이버 추천 제목 1
  - 네이버 추천 제목 2
  - 네이버 추천 제목 3
tags: [태그1, 태그2, 태그3]
---

# 네이버 추천 제목 1

> 사진 안내: 아래 [사진 N 넣을 자리] 위치에 티스토리 원문 사진을 직접 옮긴 뒤 안내 문구/URL은 삭제하세요.

원문 본문...

> [사진 1 넣을 자리: 원문 이미지]
> 원문 이미지 URL: ...
```

즉, 제목은 네이버용으로 바꿨고 본문은 티스토리 실제 원문을 기반으로 가져왔습니다.

## 3. 중요한 주의사항

### HTML 주석은 쓰지 않기

`md-to-naver-blog` v2는 MDX 파서를 쓰기 때문에 `<!-- 주석 -->`이 들어가면 오류가 날 수 있습니다.

그래서 사진 안내는 HTML 주석이 아니라 Markdown 인용문 형태로 넣었습니다.

### 사진은 직접 이동

본문에 아래처럼 표시된 부분이 있습니다.

```md
> [사진 1 넣을 자리: 원문 이미지]
> 원문 이미지 URL: https://...
```

네이버에 붙여넣은 뒤:

1. 해당 위치에 티스토리 원문 사진을 직접 삽입
2. `[사진 N 넣을 자리]` 문구 삭제
3. `원문 이미지 URL` 줄 삭제

이 순서로 처리하면 됩니다.

## 4. mtnb 변환 준비

이번 환경에서는 npm 배포판 `@jjlabsio/md-to-naver-blog@2.0.0`이 파서 오류를 냈습니다.

그래서 GitHub 저장소의 core 패키지를 로컬 빌드해서 쓰는 방식으로 맞춰두었습니다.

이미 한 번 빌드했지만, 새 PC나 새 환경에서는 아래 명령을 먼저 실행하세요.

```powershell
cd e:\git-copilot\md-to-naver-blog
npx pnpm@9.15.1 --filter @jjlabsio/md-to-naver-blog build
```

## 5. 게시글 1개 변환하기

예시: 02번 CJ 제주맥주 글 변환

```powershell
cd e:\git-copilot\dify-practice\naver_upload_test
node .\convert_mtnb_local.mjs "..\docs\naver_migration_original_posts_20260606\02_CJ_제주맥주_콜라보_후기_맥주_맛_평가_정리.md"
```

성공하면 아래 폴더에 결과가 생깁니다.

- `dify-practice/docs/naver_mtnb_outputs_20260606/`

생성 파일은 3개입니다.

- `.naver.html` : 네이버 본문용 HTML
- `.preview.html` : 브라우저에서 열어 복사 버튼으로 사용
- `.result.json` : 변환 제목, 오류, 메타 정보 확인용

변환 결과에서 `errors=0`이면 정상입니다.

## 6. 미리보기 열기

변환 후 생성된 `.preview.html` 파일을 브라우저로 열면 됩니다.

예시 파일:

```text
dify-practice/docs/naver_mtnb_outputs_20260606/02_CJ_제주맥주_콜라보_후기_맥주_맛_평가_정리.preview.html
```

미리보기 페이지에는 버튼이 3개 있습니다.

- 제목 복사
- 본문 서식 복사
- 태그 복사

## 7. 네이버 블로그에 붙여넣기

1. 네이버 블로그 글쓰기 열기
2. 미리보기에서 제목 복사
3. 네이버 제목 칸에 붙여넣기
4. 미리보기에서 본문 서식 복사
5. 네이버 본문 첫 줄에 붙여넣기
6. `[사진 N 넣을 자리]` 위치에 사진 직접 삽입
7. 사진 안내 문구와 원문 이미지 URL 삭제
8. 태그 복사 후 네이버 태그 입력란에 붙여넣기
9. 전체 문단 간격과 사진 위치만 최종 확인
10. 발행

## 8. 여러 개를 차례대로 변환하는 방법

파일명을 하나씩 바꿔서 실행하면 됩니다.

예시:

```powershell
cd e:\git-copilot\dify-practice\naver_upload_test
node .\convert_mtnb_local.mjs "..\docs\naver_migration_original_posts_20260606\03_웨딩밴드_결혼준비_코이누르_구매_후기_정리.md"
node .\convert_mtnb_local.mjs "..\docs\naver_migration_original_posts_20260606\04_판교맛집_스포키_후기_테크노밸리_건강식_추천.md"
```

## 9. 추천 작업 흐름

한 번에 29개를 다 변환하기보다 아래처럼 진행하는 것을 추천합니다.

1. Markdown 파일 하나 열기
2. 원문 내용이 잘 들어왔는지 확인
3. 필요한 경우 문장 살짝 다듬기
4. `convert_mtnb_local.mjs`로 변환
5. `.preview.html` 열기
6. 네이버에 붙여넣기
7. 사진 직접 삽입
8. 발행

## 10. 02번 테스트 결과

이미 02번 글은 테스트 변환했습니다.

- 입력: `02_CJ_제주맥주_콜라보_후기_맥주_맛_평가_정리.md`
- 결과: `dify-practice/docs/naver_mtnb_outputs_20260606/02_CJ_제주맥주_콜라보_후기_맥주_맛_평가_정리.preview.html`
- 변환 오류: `errors=0`
