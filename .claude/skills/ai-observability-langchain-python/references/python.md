> AI agents: this is one page from PostHog's docs. Full index of Markdown docs for LLMs: https://posthog.com/llms.txt

# Python - Docs

Copy page

# Python - Docs

The Python SDK makes it easy to capture events, evaluate feature flags, track errors, and more in your Python apps.

> These docs cover version `7.x` of the Python SDK, which requires Python 3.10 or higher. On Python 3.9? See [supported versions](#supported-versions).

## Installation

Terminal

PostHog AI

```bash
pip install posthog
```

**Upgrading to v6**

Version `6.x` of the PostHog Python SDK introduces a new [contexts](/docs/libraries/python.md#contexts) API and breaking changes. If you're upgrading from `5.x` to `6.x`, read the [migration guide](/tutorials/python-v6-migration.md) first to learn more.

In your app, import the `posthog` library and set your project token and host **before** making any calls.

Python

PostHog AI

```python
from posthog import Posthog
posthog = Posthog('<ph_project_token>', host='https://us.i.posthog.com')
```

> **Note:** As a rule of thumb, we do not recommend having API keys or tokens in plaintext. Setting it as an environment variable is best.

You can find your project token and instance address in the [project settings](https://app.posthog.com/project/settings) page in PostHog.

## Identifying users

> **Identifying users is required.** Backend events need a `distinct_id` to associate events with the correct user.
>
> In Python, you can do this through a context. All event captures in the same context will be tagged automatically with the correct `distinct_id`. Typically, you would set a fresh context and identify at the top of each route.
>
> Python
>
> PostHog AI
>
> ```python
> from posthog import new_context, identify_context, capture
> @app.get("/foo")
> def foo(current_user: User = Depends(get_current_user)):
>     with new_context(): # Set context at the top of a route
>         identify_context(current_user.id)
>         capture("foo_viewed")
>     return {"status": "ok"}
> ```
>
> When possible, write a small piece of **middleware** that resolves your authenticated user, wrap a context around the request, and identifies it. Every `capture()` downstream is then attributed *automatically*. The SDK's Django middleware does this automatically and you can replicate it when using the plain Python SDK.

## Capturing events

You can send custom events using `capture`:

Python

PostHog AI

```python
# Events captured with no context or explicit distinct_id are marked as personless and have an auto-generated distinct_id:
posthog.capture('some-anon-event')
from posthog import identify_context, new_context
# Use contexts to manage user identification across multiple capture calls
with new_context():
    identify_context('distinct_id_of_the_user')
    posthog.capture('user_signed_up')
    posthog.capture('user_logged_in')
    # You can also capture events with a specific distinct_id
    posthog.capture('some-custom-action', distinct_id='distinct_id_of_the_user')
```

> **Tip:** We recommend using a `[object] [verb]` format for your event names, where `[object]` is the entity that the behavior relates to, and `[verb]` is the behavior itself. For example, `project created`, `user signed up`, or `invite sent`.

> **Tip:** You can define event schemas with typed properties and generate type-safe code using [schema management](/docs/product-analytics/schema-management.md).

### Setting event properties

Optionally, you can include additional information with the event by including a [properties](/docs/data/events.md#event-properties) object:

Python

PostHog AI

```python
posthog.capture(
    "user_signed_up",
    distinct_id="distinct_id_of_the_user",
    properties={
        "login_type": "email",
        "is_free_trial": "true"
    }
)
```

### Sending page views

If you're aiming for a backend-only implementation of PostHog and won't be capturing events from your frontend, you can send `pageviews` from your backend like so:

Python

PostHog AI

```python
posthog.capture('$pageview', distinct_id="distinct_id_of_the_user", properties={'$current_url': 'https://example.com'})
```

## Person profiles and properties

The Python SDK captures identified events if the current context is identified or if you pass a distinct ID explicitly. These create [person profiles](/docs/data/persons.md). To set [person properties](/docs/data/user-properties.md) in these profiles, include them when capturing an event:

Python

PostHog AI

```python
# Passing a distinct id explicitly
posthog.capture(
    'event_name',
    distinct_id='user-distinct-id',
    properties={
        '$set': {'name': 'Max Hedgehog'},
        '$set_once': {'initial_url': '/blog'}
    }
)
# Using contexts
from posthog import new_context, identify_context
with new_context():
    identify_context('user-distinct-id')
    posthog.capture('event_name')
```

For more details on the difference between `$set` and `$set_once`, see our [person properties docs](/docs/data/user-properties.md#what-is-the-difference-between-set-and-set_once).

To capture [anonymous events](/docs/data/anonymous-vs-identified-events.md) without person profiles, set the event's `$process_person_profile` property to `False`. Events captured with no context or explicit distinct\_id are marked as personless, and will have an auto-generated distinct\_id:

Python

PostHog AI

```python
posthog.capture(
    event='event_name',
    properties={
        '$process_person_profile': False
    }
)
```

## Alias

Sometimes, you want to assign multiple distinct IDs to a single user. This is helpful when your primary distinct ID is inaccessible. For example, if a distinct ID used on the frontend is not available in your backend.

In this case, you can use `alias` to assign another distinct ID to the same user.

Python

PostHog AI

```python
posthog.alias(previous_id='distinct_id', distinct_id='alias_id')
```

We strongly recommend reading our docs on [alias](/docs/product-analytics/identify.md#alias-assigning-multiple-distinct-ids-to-the-same-user) to best understand how to correctly use this method.

## Contexts

The Python SDK uses nested contexts for managing state that's shared across events. Contexts are the recommended way to manage things like "which user is taking this action" (through `identify_context`), rather than manually passing user state through your apps stack.

When events (including exceptions) are captured in a context, the event uses the user [distinct ID](/docs/getting-started/identify-users.md), [session ID](/docs/data/sessions.md), and tags that are (optionally) set in the context. This is useful for adding properties to multiple events during a single user's interaction with your product.

You can enter a context using the `with` statement:

Python

PostHog AI

```python
from posthog import new_context, tag, set_context_session, identify_context
with new_context():
    tag("transaction_id", "abc123")
    tag("some_arbitrary_value", {"tags": "can be dicts"})
    # Sessions are UUIDv7 values and used to track a sequence of events that occur within a single user session
    # See https://posthog.com/docs/data/sessions
    set_context_session(session_id)
    # Setting the context-level distinct ID. See below for more details.
    identify_context(user_id)
    # This event is captured with the distinct ID, session ID, and tags set above
    posthog.capture("order_processed")
```

Contexts are persisted across function calls. If you enter one and then call a function and capture an event in the called function, it uses the context tags and session ID set in the parent context:

Python

PostHog AI

```python
from posthog import new_context, tag
def some_function():
    # When called from `outer_function`, this event is captured with the property some-key="value-4"
    posthog.capture("order_processed")
def outer_function():
    with new_context():
        tag("some-key", "value-4")
        some_function()
```

Contexts are nested, so tags added to a parent context are inherited by child contexts. If you set the same tag in both a parent and child context, the child context's value overrides the parent's at event capture (but the parent context won't be affected). This nesting also applies to session IDs and distinct IDs.

Python

PostHog AI

```python
from posthog import new_context, tag
with new_context():
    tag("some-key", "value-1")
    tag("some-other-key", "another-value")
    with new_context():
        tag("some-key", "value-2")
        # This event is captured with some-key="value-2" and some-other-key="another-value"
        posthog.capture("order_processed")
    # This event is captured with some-key="value-1" and some-other-key="another-value"
    posthog.capture("order_processed")
```

You can disable this nesting behavior by passing `fresh=True` to `new_context`:

Python

PostHog AI

```python
from posthog import new_context, tag
with new_context(fresh=True):
    tag("some-key", "value-2")
    # This event only has the property some-key="value-2" from the fresh context
    posthog.capture("order_processed")
```

> **Note:** Distinct IDs, session IDs, and properties passed directly to calls to `capture` and related functions override context state in the final event captured.

### Contexts and user identification

Contexts can be associated with a distinct ID by calling `posthog.identify_context`:

Python

PostHog AI

```python
from posthog import identify_context
identify_context("distinct-id")
```

Within a context associated with a distinct ID, all events captured are associated with that user. You can override the distinct ID for a specific event by passing a `distinct_id` argument to `capture`:

Python

PostHog AI

```python
from posthog import new_context, identify_context
with new_context():
    identify_context("distinct-id")
    posthog.capture("order_processed") # will be associated with distinct-id
    posthog.capture("order_processed", distinct_id="another-distinct-id") # will be associated with another-distinct-id
```

It's recommended to pass the currently active distinct ID from the frontend to the backend, using the `X-POSTHOG-DISTINCT-ID` header. If you're using our Django middleware, this is extracted and associated with the request handler context automatically.

You can read more about identifying users in the [user identification documentation](/docs/product-analytics/identify.md).

### Contexts and sessions

Contexts can be associated with a session ID by calling `posthog.set_context_session`. When linking backend events to frontend sessions, use the session ID from the frontend SDK (PostHog session IDs are UUIDv7 strings).

Python

PostHog AI

```python
from posthog import new_context, set_context_session
with new_context():
    set_context_session(request.get_header("X-POSTHOG-SESSION-ID"))
```

**Using PostHog on your frontend too?**

If you're using the PostHog JavaScript Web SDK on your frontend, it generates a session ID for you. Configure [`tracing_headers`](/docs/libraries/js/config.md#tracing-headers) for your backend hostname to add the session and distinct ID headers to browser requests automatically.

You need to extract the header in your request handler (if you're using our Django middleware integration, this happens automatically).

If you associate a context with a session, you'll be able to do things like:

-   See backend events on the session timeline when viewing session replays
-   View session replays for users that triggered a backend exception in error tracking

You can read more about sessions in the [session tracking](/docs/data/sessions.md) documentation.

### Exception capture

By default exceptions raised within a context are captured and available in the [error tracking](/docs/error-tracking.md) dashboard. You can override this behavior by passing `capture_exceptions=False` to `new_context`:

Python

PostHog AI

```python
from posthog import new_context, tag
with new_context(capture_exceptions=False):
    tag("transaction_id", "abc123")
    tag("some_arbitrary_value", {"tags": "can be dicts"})
    # This event will be captured with the tags set above
    posthog.capture("order_processed")
    # This exception will not be captured
    raise Exception("Order processing failed")
```

### Decorating functions

The SDK exposes a function decorator. It takes the same `fresh` and `capture_exceptions` arguments as `new_context` and provides a handy way to mark a whole function as being in a new context. For example:

Python

PostHog AI

```python
from posthog import scoped, identify_context
@scoped(fresh=True)
def process_order(user, order_id):
    identify_context(user.distinct_id)
    posthog.capture("order_processed") # Associated with the user
    raise Exception("Order processing failed") # This exception is also captured and associated with the user
```

## Group analytics

Group analytics allows you to associate an event with a group (e.g. teams, organizations, etc.). Read the [Group Analytics](/docs/user-guides/group-analytics.md) guide for more information.

> **Note:** This is a paid feature and is not available on the open-source or free cloud plan. Learn more on our [pricing page](/pricing.md).

To capture an event and associate it with a group:

Python

PostHog AI

```python
posthog.capture('some_event', groups={'company': 'company_id_in_your_db'})
```

To update properties on a group:

Python

PostHog AI

```python
posthog.group_identify('company', 'company_id_in_your_db', {
    'name': 'Awesome Inc.',
    'employees': 11
})
```

The `name` is a special property which is used in the PostHog UI for the name of the group. If you don't specify a `name` property, the group ID will be used instead.

## Feature flags

PostHog's [feature flags](/docs/feature-flags.md) enable you to safely deploy and roll back new features as well as target specific users and groups with them.

There are two steps to implement feature flags in Python:

### Step 1: Evaluate flags once

Call `posthog.evaluate_flags()` once for the user, then read values from the returned snapshot.

#### Boolean feature flags

Python

PostHog AI

```python
flags = posthog.evaluate_flags("distinct_id_of_your_user")
if flags.is_enabled("flag-key"):
    # Do something differently for this user
    # Optional: fetch the payload
    matched_flag_payload = flags.get_flag_payload("flag-key")
```

#### Multivariate feature flags

Python

PostHog AI

```python
flags = posthog.evaluate_flags("distinct_id_of_your_user")
enabled_variant = flags.get_flag("flag-key")
if enabled_variant == "variant-key":  # replace "variant-key" with the key of your variant
    # Do something differently for this user
    # Optional: fetch the payload
    matched_flag_payload = flags.get_flag_payload("flag-key")
```

`flags.get_flag()` returns the variant string for multivariate flags, `True` for enabled boolean flags, `False` for disabled flags, and `None` when the flag wasn't returned by the evaluation.

> **Note:** `posthog.feature_enabled()`, `posthog.get_feature_flag()`, `posthog.get_feature_flag_payload()`, and `posthog.capture(send_feature_flags=True)` still work during the migration period, but they're deprecated. Prefer `posthog.evaluate_flags()` for new code.

### Step 2: Include feature flag information when capturing events

If you want use your feature flag to breakdown or filter events in your [insights](/docs/product-analytics/insights.md), you'll need to include feature flag information in those events. This ensures that the feature flag value is attributed correctly to the event.

> **Note:** This step is only required for events captured using our server-side SDKs or [API](/docs/api.md).

There are two methods you can use to include feature flag information in your events:

#### Method 1: Pass the evaluated flags snapshot to `capture()`

Pass the same `flags` object that you used for branching. This attaches the exact flag values from that evaluation and doesn't make another `/flags` request.

Python

PostHog AI

```python
flags = posthog.evaluate_flags("distinct_id_of_your_user")
if flags.is_enabled("flag-key"):
    # Do something differently for this user
    pass
posthog.capture(
    "event_name",
    distinct_id="distinct_id_of_your_user",
    flags=flags,
)
```

By default, this attaches every flag in the snapshot using `$feature/<flag-key>` properties and `$active_feature_flags`.

To reduce event property bloat, pass a filtered snapshot:

Python

PostHog AI

```python
# Attach only flags accessed with is_enabled() or get_flag() before this call
posthog.capture(
    "event_name",
    distinct_id="distinct_id_of_your_user",
    flags=flags.only_accessed(),
)
# Attach only specific flags
posthog.capture(
    "event_name",
    distinct_id="distinct_id_of_your_user",
    flags=flags.only(["checkout-flow", "new-dashboard"]),
)
```

`only_accessed()` is order-dependent. If you call it before accessing any flags with `is_enabled()` or `get_flag()`, no feature flag properties are attached.

#### Method 2: Include the `$feature/feature_flag_name` property manually

In the event properties, include `$feature/feature_flag_name: variant_key`:

Python

PostHog AI

```python
posthog.capture(
    "event_name",
    distinct_id="distinct_id_of_the_user",
    properties={
        # Replace feature-flag-key with your flag key and "variant-key" with the key of your variant
        "$feature/feature-flag-key": "variant-key",
    },
)
```

### Evaluating only specific flags

By default, `posthog.evaluate_flags()` evaluates every flag for the user. If you only need a few flags, pass `flag_keys` to request only those flags:

Python

PostHog AI

```python
flags = posthog.evaluate_flags(
    "distinct_id_of_your_user",
    flag_keys=["checkout-flow", "new-dashboard"],
)
```

### Sending `$feature_flag_called` events

Capturing `$feature_flag_called` events enables PostHog to know when a flag was accessed by a user and provide [analytics and insights](/docs/product-analytics/insights.md) on the flag. With `posthog.evaluate_flags()`, the SDK sends this event when you call `flags.is_enabled()` or `flags.get_flag()` for a flag.

The SDK deduplicates these events per `(distinct_id, flag, value)` in a local cache. If you reinitialize the PostHog client, the cache resets and `$feature_flag_called` events may be sent again. PostHog handles duplicates, so duplicate `$feature_flag_called` events don't affect your analytics.

`flags.get_flag_payload()` doesn't send `$feature_flag_called` events and doesn't count as an access for `only_accessed()`.

### Advanced: Overriding server properties

Sometimes, you may want to evaluate feature flags using [person properties](/docs/product-analytics/person-properties.md), [groups](/docs/product-analytics/group-analytics.md), or group properties that haven't been ingested yet, or were set incorrectly earlier.

You can provide properties to evaluate the flag with by using the `person properties`, `groups`, and `group properties` arguments. PostHog will then use these values to evaluate the flag, instead of any properties currently stored on your PostHog server.

For example:

Python

PostHog AI

```python
flags = posthog.evaluate_flags(
    "distinct_id_of_the_user",
    person_properties={"property_name": "value"},
    groups={
        "your_group_type": "your_group_id",
        "another_group_type": "your_group_id",
    },
    group_properties={
        "your_group_type": {"group_property_name": "value"},
        "another_group_type": {"group_property_name": "value"},
    },
)
if flags.is_enabled("flag-key"):
    # Do something differently for this user
```

### Overriding GeoIP properties

By default, a user's GeoIP properties are set using the IP address they use to capture events on the frontend. You may want to override the these properties when evaluating feature flags. A common reason to do this is when you're not using PostHog on your frontend, so the user has no GeoIP properties.

You can override GeoIP properties by including them in the `person_properties` parameter when evaluating feature flags. This is useful when you're evaluating flags on your backend and want to use the client's location instead of your server's location.

The following GeoIP properties can be overridden:

-   `$geoip_country_code`
-   `$geoip_country_name`
-   `$geoip_city_name`
-   `$geoip_city_confidence`
-   `$geoip_continent_code`
-   `$geoip_continent_name`
-   `$geoip_latitude`
-   `$geoip_longitude`
-   `$geoip_postal_code`
-   `$geoip_subdivision_1_code`
-   `$geoip_subdivision_1_name`
-   `$geoip_subdivision_2_code`
-   `$geoip_subdivision_2_name`
-   `$geoip_subdivision_3_code`
-   `$geoip_subdivision_3_name`
-   `$geoip_time_zone`

Simply include any of these properties in the `person_properties` parameter alongside your other person properties when calling feature flags.

### Request timeout

You can configure the `feature_flags_request_timeout_seconds` parameter when initializing your PostHog client to set a flag request timeout. This helps prevent your code from being blocked if PostHog's servers are too slow to respond. By default, this is set to 3 seconds.

Python

PostHog AI

```python
posthog = Posthog(
    "<ph_project_token>",
    host="https://us.i.posthog.com",
    feature_flags_request_timeout_seconds=3,  # Time in seconds. Defaults to 3.
)
```

### Local evaluation

Evaluating feature flags requires making a request to PostHog for each flag. However, you can improve performance by evaluating flags locally. Instead of making a request for each flag, PostHog will periodically request and store feature flag definitions locally, enabling you to evaluate flags without making additional requests.

It is best practice to use local evaluation flags when possible, since this enables you to resolve flags faster and with fewer API calls.

For details on how to implement local evaluation, see our [local evaluation guide](/docs/feature-flags/local-evaluation.md).

#### Distributed environments

In multi-worker or edge environments, you can implement custom caching for flag definitions using Redis, Cloudflare KV, or other storage backends. This enables sharing definitions across workers and coordinating fetches. See our guide for [local evaluation in distributed environments](/docs/feature-flags/local-evaluation/distributed-environments?tab=Python.md) for details.

## Experiments (A/B tests)

Since [experiments](/docs/experiments/start-here.md) use feature flags, the code for running an experiment is very similar to the feature flags code:

Python

PostHog AI

```python
flags = posthog.evaluate_flags("user_distinct_id")
variant = flags.get_flag("experiment-feature-flag-key")
if variant == "variant-name":
    # Do something
```

It's also possible to [run experiments without using feature flags](/docs/experiments/running-experiments-without-feature-flags.md).

## AI Observability

Our Python SDK includes a built-in AI Observability feature. It enables you to capture LLM usage, performance, and more. Check out our [analytics docs](/docs/ai-observability.md) for more details on setting it up.

## Error tracking

You can [autocapture exceptions](/docs/error-tracking/installation.md) by setting the `enable_exception_autocapture` argument to `True` when initializing the PostHog client.

Python

PostHog AI

```python
from posthog import Posthog
posthog = Posthog("<ph_project_token>", enable_exception_autocapture=True, ...)
```

You can also manually capture exceptions using the `capture_exception` method:

Python

PostHog AI

```python
posthog.capture_exception(e, distinct_id='user_distinct_id', properties=additional_properties)
```

Contexts automatically capture exceptions thrown inside them, unless disable it by passing `capture_exceptions=False` to `new_context()`.

### Code variables capture

The Python SDK can automatically capture the state of local variables when an exception occurs. This gives you a debugger-like view of your application state at the time of the error:

Python

PostHog AI

```python
posthog = Posthog(
    "<ph_project_token>",
    enable_exception_autocapture=True,
    capture_exception_code_variables=True,
)
```

You can configure which variables are captured, masked, or ignored. See the [code variables documentation](/docs/error-tracking/code-variables/python.md) for detailed configuration options.

## GeoIP properties

Before posthog-python v3.0, we added GeoIP properties to all incoming events by default. We also used these properties for feature flag evaluation, based on the IP address of the request. This isn't ideal since they are created based on your server IP address, rather than the user's, leading to incorrect location resolution.

As of posthog-python v3.0, the default now is to disregard the server IP, not add the GeoIP properties, and not use the values for feature flag evaluations.

You can go back to previous behavior by doing setting the `disable_geoip` argument in your initialization to `False`:

Python

PostHog AI

```python
posthog = Posthog('api_key', disable_geoip=False)
```

The list of properties that this overrides:

1.  `$geoip_city_name`
2.  `$geoip_country_name`
3.  `$geoip_country_code`
4.  `$geoip_continent_name`
5.  `$geoip_continent_code`
6.  `$geoip_postal_code`
7.  `$geoip_time_zone`

You can also explicitly chose to enable or disable GeoIP for a single capture request like so:

Python

PostHog AI

```python
posthog.capture('test_event', disable_geoip=True|False)
```

## Debug mode

If you're not seeing the expected events being captured, the feature flags being evaluated, or the surveys being shown, you can enable debug mode to see what's happening.

You can enable debug mode by setting the `debug` option to `True` in the `PostHog` object. This will enable verbose logs about the inner workings of the SDK.

Python

PostHog AI

```python
posthog.debug = True
```

## Disabling requests during tests

You can disable requests during tests by setting the `disabled` option to `True` in the `PostHog` object. This means no events will be captured or no requests will be sent to PostHog.

Python

PostHog AI

```python
if settings.TEST:
    posthog.disabled = True
```

## Connection configuration

The SDK uses HTTP connection pooling internally for better performance. These settings typically need not be changed, but in some environments, such as when running behind NAT gateways, pooled connections may be terminated non-gracefully, causing request failures.

You can configure connection behavior in several ways. The following settings should be called during initialization, before any API requests are made.

### Enable TCP keepalive

TCP keepalive probes help prevent idle connections from being dropped by network infrastructure. This is the recommended approach for most cases where idle connections are terminated.

Python

PostHog AI

```python
import posthog
posthog.enable_keep_alive()
```

This enables TCP keepalive with sensible defaults (60 second idle time, 60 second probe interval, 3 probes before timeout).

### Disable connection pooling

If you need each request to use a fresh connection, you can disable connection reuse entirely. This will incur additional overhead per request but may be desirable in some circumstances.

Python

PostHog AI

```python
import posthog
posthog.disable_connection_reuse()
```

### Custom HTTP socket options

For advanced use cases, you can configure arbitrary socket options on the underlying HTTP connection.

Python

PostHog AI

```python
import socket
import posthog
posthog.set_socket_options([
    (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
    # Add additional socket options as needed
])
```

Pass `None` to `set_socket_options()` to reset to default behavior.

## Filtering or modifying events before sending

Use `before_send` to modify or drop events before they are queued for delivery. Return the modified event dictionary to send it, or `None` to drop it.

Python

PostHog AI

```python
from typing import Any
import posthog
def scrub_pii(event: dict[str, Any]) -> dict[str, Any] | None:
    properties = event.get("properties", {})
    if "email" in properties:
        email = properties["email"]
        properties["email"] = f"***@{email.split('@', 1)[1]}" if "@" in email else "***"
    if event.get("event") == "test_event":
        return None
    return event
client = posthog.Client(
    "<ph_project_api_key>",
    before_send=scrub_pii,
)
```

If your callback raises an exception, the SDK logs the error and continues with the original unmodified event.

## Historical migrations

You can use the Python or Node SDK to run [historical migrations](/docs/migrate.md) of data into PostHog. To do so, set the `historical_migration` option to `true` when initializing the client.

PostHog AI

### Python

```python
from posthog import Posthog
from datetime import datetime
posthog = Posthog(
    '<ph_project_token>',
    host='https://us.i.posthog.com',
    debug=True,
    historical_migration=True
)
events = [
  {
    "event": "batched_event_name",
    "properties": {
      "distinct_id": "user_id",
      "timestamp": datetime.fromisoformat("2024-04-02T12:00:00")
    }
  },
  {
    "event": "batched_event_name",
    "properties": {
      "distinct_id": "used_id",
      "timestamp": datetime.fromisoformat("2024-04-02T12:00:00")
    }
  }
]
for event in events:
  posthog.capture(
    distinct_id=event["properties"]["distinct_id"],
    event=event["event"],
    properties=event["properties"],
    timestamp=event["properties"]["timestamp"],
  )
```

### Node.js

```javascript
import { PostHog } from 'posthog-node'
const client = new PostHog(
    '<ph_project_token>',
    {
      host: 'https://us.i.posthog.com',
      historicalMigration: true
    }
)
client.debug()
client.capture({
    event: "batched_event_name",
    distinctId: "user_id",
    properties: {},
    timestamp: "2024-04-03T12:00:00Z"
})
client.capture({
    event: "batched_event_name",
    distinctId: "user_id",
    properties: {},
    timestamp: "2024-04-03T13:00:00Z"
})
await client.shutdown()
```

## Serverless environments (Render/Lambda/...)

By default, the library buffers events before sending them to the capture endpoint, for better performance. This can lead to lost events in serverless environments, if the Python process is terminated by the platform before the buffer is fully flushed. To avoid this, you can either:

-   Ensure that `posthog.shutdown()` is called after processing every request by adding a middleware to your server. This allows `posthog.capture()` to remain asynchronous for better performance. `posthog.shutdown()` is blocking.
-   Enable the `sync_mode` option when initializing the client, so that all calls to `posthog.capture()` become synchronous.

## Django

See our [Django docs](/docs/libraries/django.md) for how to set up PostHog in Django. Our library includes a [contexts middleware](/docs/libraries/django.md#django-contexts-middleware) that can automatically capture distinct IDs, session IDs, and other properties you can set up with tags.

## Alternative name

As our open source project [PostHog](https://github.com/PostHog/posthog) shares the same module name, we created a special `posthoganalytics` package, mostly for internal use to avoid module collision. It is the exact same.

## Thank you

This library is largely based on the `analytics-python` package.

## Supported versions

These docs cover version `7.x` of the PostHog Python SDK, which requires Python 3.10 or higher. Python 3.9 is no longer supported on `7.x.x` and higher — pin to the 6.x line with `pip install 'posthog<7'`, where `6.9.3` is the final release.

Everything on this page works the same way on `6.9.3`. Event capture, the context API (`new_context`, `identify_context`, `set_context_session`), and `PosthogContextMiddleware` are identical on `6.9.3` and `7.0.0` — `7.0.0` only dropped Python 3.9 and bumped the optional LLM provider SDKs. That includes the middleware identifying the request context from the `X-POSTHOG-DISTINCT-ID` header and falling back to the authenticated user, which behaves the same across both lines.

Later `7.x` releases add what the 6.x line does not receive, such as the Celery integration, tracing header sanitization, and `set_context_device_id`. They also changed the middleware's own captured properties: `7.x` sends the request IP as `$ip`, where `6.9.3` sends it as `$ip_address`, and `7.x` additionally captures `$request_path`, `$raw_user_agent`, and the authenticated user's `email`.

### Still have questions?

Ask PostHog AI

### Was this page useful?

HelpfulCould be better