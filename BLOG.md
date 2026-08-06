# Tentative blog series

## Zeta: A Harness for Agents That Change Their Own Environment

Most agent frameworks focus on calling tools from a fixed prompt and configuration. Zeta lets agents transform content, create capabilities, and leave an inspectable record of each change.

## The Content Graph Is the Agent's Working Memory

Larger context windows do not solve the problem of deciding what information matters. Zeta gives agents a revisioned workspace where they can query, transform, promote, and restore content.

## Making RLM Computation Durable

RLMs show how a model can use code and child-model calls to work over external context. Zeta adds durable commits to this computation. Each useful result becomes an addressable revision. Zeta can trace, retry, restore, or promote the revision. An old computation cannot replace newer state.

## Let the Agent Create the Tool It Needs

Some tasks reveal their required tools only after execution begins. Zeta lets an agent turn code into a validated, typed capability that becomes available at a clear generation boundary.

## An Event-Driven Interface Between Agents and the World

`publish_event` lets agents produce declared effects, while `query_log` lets them inspect what already happened. Connectors can turn these events into notifications, watch complications, workflows, or other external actions.

## Self-Modifying Agents Need History, Not Handcuffs

Agents that can edit and execute code already have significant power, so a simple ban on self-modification is not enough. Zeta makes changes explicit, immutable, validated, reversible, and visible to both the agent and the operator.
