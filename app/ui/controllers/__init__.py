"""MainWindow 分解（Phase 4契約）由来のコントローラ群。

`app/ui/main_window.py` から責務ごとに切り出したプレーンクラス（QObject 非継承）を
配置するパッケージ。各コントローラはコンストラクタで `window`/`scene` 等の参照を
受け取り、MainWindow から呼び出される。ロジックは移動のみで変更しない。
"""
