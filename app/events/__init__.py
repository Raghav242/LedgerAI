"""Reliable events: transactional outbox -> RabbitMQ -> idempotent consumers.

Delivery is AT-LEAST-ONCE end to end. The publisher may publish an event more than once
(e.g. it crashes after the broker confirmed but before it recorded that), and the broker
may deliver a message more than once (e.g. the consumer crashes before acknowledging).
Consumers make the *effect* happen once by deduplicating on (event_id, consumer_name).
"""
