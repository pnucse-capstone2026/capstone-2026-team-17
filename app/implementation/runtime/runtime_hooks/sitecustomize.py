"""오케스트레이터가 시작한 멤버 Python 프로세스에만 적용되는 실행 훅."""


from app.config import settings
from app.implementation.runtime.docker_path_adapter import install as install_docker_paths

install_docker_paths()
if settings.easydep_fixed_linux_runner == "1":
    from app.implementation.runtime.runner_compat import install as install_runner_compat

    install_runner_compat()
