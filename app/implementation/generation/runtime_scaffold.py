from __future__ import annotations

from pathlib import Path


def write_jackson_runtime_configuration(application: Path, base_package: str) -> Path:
    """Write the deterministic Jackson module configuration required by generated DTOs."""
    config_root = (
        application
        / "src"
        / "main"
        / "java"
        / Path(base_package.replace(".", "/"))
        / "config"
    )
    config_root.mkdir(parents=True, exist_ok=True)
    target = config_root / "JacksonConfiguration.java"
    target.write_text(
        f"package {base_package}.config;\n\n"
        "import com.fasterxml.jackson.databind.Module;\n"
        "import org.openapitools.jackson.nullable.JsonNullableModule;\n"
        "import org.springframework.context.annotation.Bean;\n"
        "import org.springframework.context.annotation.Configuration;\n\n"
        "@Configuration\n"
        "public class JacksonConfiguration {\n"
        "    @Bean\n"
        "    public Module jsonNullableModule() {\n"
        "        return new JsonNullableModule();\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    return target
