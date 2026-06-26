def clear_project_graph(driver, project_name: str):
    with driver.session() as session:
        session.run("MATCH (n {project: $p}) DETACH DELETE n", p=project_name)


def get_all_domains(driver, project: str) -> list[dict]:
    with driver.session() as session:
        result = session.run(
            "MATCH (d:Domain {project: $p}) RETURN d.name AS name, d.description AS description",
            p=project,
        )
        return [dict(r) for r in result]


def get_bounded_contexts_for_domain(driver, project: str, domain_name: str) -> list[dict]:
    with driver.session() as session:
        result = session.run(
            """
            MATCH (d:Domain {project: $p, name: $dn})-[:CONTAINS]->(sd:Subdomain)-[:CONTAINS]->(bc:BoundedContext)
            RETURN bc.name AS name, bc.description AS description,
                   bc.ubiquitous_language AS ubiquitous_language,
                   sd.name AS subdomain
            """,
            p=project, dn=domain_name,
        )
        return [dict(r) for r in result]


def get_components_for_context(driver, project: str, context_name: str) -> list[dict]:
    with driver.session() as session:
        result = session.run(
            """
            MATCH (c:Component {project: $p})-[:IMPLEMENTS]->(bc:BoundedContext {name: $cn})
            RETURN c.name AS name, c.type AS type, c.description AS description
            """,
            p=project, cn=context_name,
        )
        return [dict(r) for r in result]


def get_full_ddd_summary(driver, project: str) -> dict:
    with driver.session() as session:
        def count(label):
            r = session.run(f"MATCH (n:{label} {{project: $p}}) RETURN count(n) AS c", p=project)
            return r.single()["c"]

        value_streams = [
            dict(r) for r in session.run(
                "MATCH (vs:ValueStream {project: $p}) RETURN vs.name AS name, vs.description AS description",
                p=project,
            )
        ]
        domains = [
            dict(r) for r in session.run(
                "MATCH (d:Domain {project: $p}) RETURN d.name AS name, d.description AS description",
                p=project,
            )
        ]
        bounded_contexts = [
            dict(r) for r in session.run(
                """
                MATCH (bc:BoundedContext {project: $p})
                OPTIONAL MATCH (sd:Subdomain)-[:CONTAINS]->(bc)
                RETURN bc.name AS name, bc.description AS description,
                       bc.ubiquitous_language AS ubiquitous_language,
                       sd.name AS subdomain
                """,
                p=project,
            )
        ]
        components = [
            dict(r) for r in session.run(
                """
                MATCH (c:Component {project: $p})
                OPTIONAL MATCH (c)-[:IMPLEMENTS]->(bc:BoundedContext)
                RETURN c.name AS name, c.type AS type, c.description AS description,
                       bc.name AS bounded_context
                """,
                p=project,
            )
        ]

        return {
            "value_streams": value_streams,
            "domains": domains,
            "bounded_contexts": bounded_contexts,
            "components": components,
            "counts": {
                "value_streams": len(value_streams),
                "domains": len(domains),
                "bounded_contexts": len(bounded_contexts),
                "components": len(components),
                "capabilities": count("Capability"),
                "contracts": count("Contract"),
                "jms_queues": count("JmsQueue"),
            },
        }


def get_jms_topology(driver, project: str) -> list[dict]:
    with driver.session() as session:
        result = session.run(
            """
            MATCH (q:JmsQueue {project: $p})
            OPTIONAL MATCH (producer:Component)-[:PRODUCES_TO]->(q)
            OPTIONAL MATCH (consumer:Component)-[:CONSUMES_FROM]->(q)
            RETURN q.name AS queue_name,
                   q.description AS description,
                   q.message_type AS message_type,
                   collect(DISTINCT producer.name) AS producers,
                   collect(DISTINCT consumer.name) AS consumers
            """,
            p=project,
        )
        return [dict(r) for r in result]
