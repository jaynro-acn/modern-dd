from typing import Tuple


class FileClassifier:
    BOILERPLATE_ANNOTATIONS = {
        "Getter", "Setter", "Data", "Builder",
        "NoArgsConstructor", "AllArgsConstructor",
        "ToString", "EqualsAndHashCode",
    }
    LISTENER_ANNOTATIONS = {"JmsListener", "MessageDriven", "ActivationConfigProperty"}
    SERVICE_ANNOTATIONS = {"Service", "Stateless", "Stateful", "Singleton"}
    CONFIG_ANNOTATIONS = {"Configuration", "Bean", "ConfigurationProperties"}
    REPOSITORY_ANNOTATIONS = {"Repository"}

    def classify(self, file_info: dict) -> Tuple[str, str]:
        """Return (category, model_tier) for a parsed Java file info dict."""
        annotations = set(file_info.get("annotations", []))
        class_name = file_info.get("class_name", "")
        extends = file_info.get("extends") or ""
        implements = set(file_info.get("implements", []))
        method_count = file_info.get("method_count", 0)

        if file_info.get("is_enum"):
            return ("enum", "fast")

        if extends and ("Exception" in extends or "Error" in extends):
            return ("exception", "fast")

        if file_info.get("is_interface"):
            return ("interface", "fast")

        if annotations & self.LISTENER_ANNOTATIONS or "MessageListener" in implements:
            return ("listener", "smart")

        if (annotations & self.CONFIG_ANNOTATIONS
                or class_name.endswith(("Config", "Configuration"))):
            return ("config", "fast")

        if (annotations & self.REPOSITORY_ANNOTATIONS
                or extends.endswith(("Repository", "DAO", "Dao"))
                or class_name.endswith(("Repository", "DAO", "Dao"))):
            return ("repository", "fast")

        # DTO/POJO: has boilerplate annotations and few real methods
        has_boilerplate = bool(annotations & self.BOILERPLATE_ANNOTATIONS)
        if has_boilerplate and method_count < 5:
            return ("dto", "fast")

        if annotations & self.SERVICE_ANNOTATIONS:
            if method_count > 2:
                return ("service", "smart")
            return ("service", "fast")

        # Plain class with substantial logic
        if method_count > 3 and not (annotations & self.CONFIG_ANNOTATIONS):
            return ("domain", "smart")

        return ("unknown", "fast")
