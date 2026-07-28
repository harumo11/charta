"""エージェント制御サーバ（MCP）。

外部の AI エージェントが動作中の charta を操作し、キャンバスを画像として読めるようにする層。

構成（詳細は `.claude/working/architecture/agent.md`）::

    schema.py     Qt 非依存 — エージェント向けスキーマの自動生成
    validate.py   Qt 非依存 — プロパティ検証と自己修正可能なエラー整形
    render.py     レンダリング（クリーン図/実ウィンドウ）と注釈オーバーレイ
    api.py        AgentAPI — GUI スレッド上の公開ファサード
    host.py       QLocalServer + NDJSON JSON-RPC + busy ゲート
    jobs.py       長時間処理（SAM3）のジョブ管理
    exec_env.py   charta_exec の名前空間構築と実行

MCP プロトコル層は本体に含めない。別プロセスの `tools/charta_mcp.py` が
Unix ドメインソケット越しに `host.py` と話す。
"""
