from pathlib import Path


def get_project_root() -> Path:
    """
    获取 `script/py/crt` 项目根目录。

    说明：
    - 以本文件所在路径为锚点推导，不依赖当前工作目录。
    - 可确保无论从哪里启动进程，目录解析都固定在 crt 根目录下。
    """
    return Path(__file__).resolve().parents[2]


def resolve_project_path(path: str | Path) -> Path:
    """
    将路径解析为项目根目录下的绝对路径。

    规则：
    - 绝对路径：直接返回；
    - 相对路径：拼接到 `get_project_root()` 下。
    """
    p = Path(path).expanduser()
    if p.is_absolute():
        return p
    return get_project_root() / p

