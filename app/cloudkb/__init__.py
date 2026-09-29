"""배포 에이전트 — 클라우드 지식베이스와 배포 계획 구성기.

2026-07-25에 별도 저장소(agent-sdk)에서 합류했다. 안의 패키지들(`*kb`·`kbcommon`·
`nim_agent`)은 그 저장소의 구조를 그대로 유지한다 — `kbcommon.artifact`의
`REPO_ROOT`가 이 디렉터리를 가리키고 `data/`·`output/`이 그 아래에 있다.

임포트 규약은 `tests/test_architecture.py`가 AST로 지킨다.

## 이 코드의 이력은 main에 없다

main에는 squash로 한 커밋만 남겼다. 원본 커밋 198개는 태그
**`agent-sdk-history`**가 가리키는 곳에 있고, agent-sdk 저장소에는 리모트가 없었으
므로 **그 태그가 유일본이다.** 이 저장소의 문서 정책(`CLAUDE.md`)이 "커밋 메시지 =
변경 기록"이라, 여기 코드가 왜 이 모양인지는 대부분 그 커밋들에만 적혀 있다.

    git log --oneline agent-sdk-history          # 커밋 198개
    git log agent-sdk-history -- app/cloudkb/costkb/cli.py   # 파일 하나의 내력
    git show <sha>                               # 판단 근거는 본문에 있다

경로는 이미 `app/cloudkb/`으로 옮겨 둔 뒤라 `--follow` 없이 그대로 따라간다.
**태그를 지우면 이력이 사라진다.**
"""
