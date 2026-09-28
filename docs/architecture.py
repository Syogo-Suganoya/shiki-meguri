"""シキめぐりのアーキテクチャ図を生成する。

    docker compose --profile docs run --rm docs

出力: docs/architecture.png

技術スタックが分かることを目的とし、エージェント内部の委譲や権限の別は描かない。
"""

from diagrams import Cluster, Diagram, Edge
from diagrams.gcp.compute import Run
from diagrams.gcp.database import Firestore
from diagrams.gcp.ml import AIPlatform
from diagrams.gcp.operations import Logging
from diagrams.gcp.security import KeyManagementService
from diagrams.gcp.storage import Storage
from diagrams.onprem.client import Users
from diagrams.programming.language import Python

FONT = "Noto Sans CJK JP"

graph_attr = {
    "fontname": FONT,
    "fontsize": "14",
    "bgcolor": "white",
    "splines": "spline",
    "pad": "0.5",
    "nodesep": "0.8",
    "ranksep": "1.3",
}
node_attr = {"fontname": FONT, "fontsize": "11"}
edge_attr = {"fontname": FONT, "fontsize": "10", "color": "#5a5a6e"}
cluster_attr = {
    "fontname": FONT,
    "fontsize": "12",
    "style": "rounded",
    "pencolor": "#9c98ad",
}

with Diagram(
    "シキめぐり — アーキテクチャ",
    filename="docs/architecture",
    outformat="png",
    show=False,
    direction="TB",
    graph_attr=graph_attr,
    node_attr=node_attr,
    edge_attr=edge_attr,
):
    with Cluster("クライアント", graph_attr=cluster_attr):
        client = Users("Web PWA\n自前チャットUI")

    # 本番は api と agent を分けず、1 つのサービスの同じプロセスで動かす
    # （AGENT_TRANSPORT=inproc）。分ける構成は開発用に残してあるだけなので描かない。
    with Cluster("Cloud Run（shiki-api）", graph_attr=cluster_attr):
        api = Run("API・画面の配信\nFastAPI")
        agent = Python("Orchestrator\n衣装・手配・経路・返却監視")

    with Cluster("外部API", graph_attr=cluster_attr):
        externals = [
            AIPlatform("Gemini API"),
            Storage("駅すぱあと API\nMCPサーバー"),
            Storage("レンタル事業者API\n（モック）"),
        ]

    with Cluster("データ", graph_attr=cluster_attr):
        firestore = Firestore("Firestore\n式・会話履歴")
        logging = Logging("Cloud Logging\n監査ログ")
        secrets = KeyManagementService("Secret Manager")

    client >> Edge(label="発言") >> api >> Edge(label="同じプロセス") >> agent
    # 端末へのプッシュは使わず、エージェント起点の通知も画面が取りに来る。
    api >> Edge(label="通知（アプリ内・ポーリング）", style="dashed") >> client
    agent >> externals
    agent >> Edge(style="dashed") >> [firestore, logging]
    secrets >> Edge(style="dashed") >> agent
