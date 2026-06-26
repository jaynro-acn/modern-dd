NODE_LABELS = [
    "ValueStream",
    "Capability",
    "Domain",
    "Subdomain",
    "BoundedContext",
    "Component",
    "Contract",
    "Interface",
    "JmsQueue",
    "Feature",
]


def setup_schema(driver):
    with driver.session() as session:
        for label in NODE_LABELS:
            session.run(
                f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.entityId IS UNIQUE"
            )
