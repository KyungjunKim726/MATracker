"""서비스 진입점.

    python main.py run       # 스케줄러 + 텔레그램 롱폴링
    python main.py --help    # 사용 가능한 명령 전체
"""

from __future__ import annotations

from cli import main

if __name__ == "__main__":
    raise SystemExit(main())
