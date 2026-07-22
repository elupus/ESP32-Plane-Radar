#pragma once

namespace services::route {

/**
 * Looks up and caches origin/destination routes for callsigns currently
 * seen by services::adsb. Performs at most one network lookup per call and
 * only when its own internal interval is due, so it's safe (and intended)
 * to call this every tick of the background network task. Performs
 * blocking network I/O — do not call from the render loop.
 */
void poll();

/**
 * Compact "ORIG>DEST" route string for callsign (IATA codes, falling back
 * to ICAO per side when an airport has no IATA code), or nullptr if the
 * route isn't known yet or genuinely has no schedule data. Only safe to
 * call while holding services::data::lock().
 */
const char* routeFor(const char* callsign);

}  // namespace services::route
