# Solution Overview

## What We Built

AI Ambulance-to-Hospital Coordinator is a browser dashboard backed by a FastAPI service. It turns an incident condition and current hospital data into an explainable destination recommendation based on fastest expected treatment.

## How It Works

1. The dispatcher starts a trip and selects the suspected patient condition.
2. The service maps that condition to required capabilities such as ICU capacity or a specialist.
3. Hospitals that fail hard clinical or capacity constraints are removed.
4. The remaining hospitals receive ETA, capacity, specialist, and treatment-time scores.
5. The ranked recommendation and reasons are returned to the dashboard.
6. Accepting a recommendation reserves capacity and sends a hospital pre-alert.
7. A background trip loop updates position and checks for changed hospital conditions.
8. If the current destination becomes unsuitable, the service suggests a reroute and releases the old reservation when accepted.

## What Makes It Different

The prototype optimizes time to treatment instead of distance and exposes the comparison behind the decision. It also models capacity as a shared resource, so multiple active ambulances can contend for the same hospital beds in the demo.

## IBM Technologies Used

No IBM service is claimed in this prototype. The implementation is intentionally self-contained so judges can run it without external credentials. The architecture leaves clear integration points for hospital information systems and an enterprise AI service in a future production version.
