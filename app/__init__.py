"""风机叶片裂纹复核 API。

数据模型：
- 巡检观察 Observation：距叶根起止距离 + 顺时针钟位区间 + 等级 + 置信度
- 裂纹轨迹 Trajectory：在径向与钟位（环向）上同时相交的观察合并为同一连通簇
"""

__all__ = ["create_app"]


def create_app():
    from .main import app

    return app
