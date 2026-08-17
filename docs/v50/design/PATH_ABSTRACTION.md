# SAFE AGENT V50 Path Abstraction

## Purpose

Centralize runtime filesystem ownership.

## Rules

Runtime writers must not construct state paths directly.

All mutable paths must resolve through:

- runtime.paths.STATE_ROOT
- runtime.paths.AUDIT_ROOT
- runtime.paths.SNAPSHOT_ROOT
- runtime.paths.CACHE_ROOT

## Benefits

- single migration point
- easier testing
- reduced accidental source mutation
- deterministic runtime layout

## Migration

Existing V49 writers will be redirected incrementally.
