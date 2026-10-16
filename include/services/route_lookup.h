#pragma once

namespace services::route {

struct Route {
  /** Compact "ORIG>DEST" (IATA codes, falling back to ICAO per side). */
  char text[16];
  /** Airport codes per side (may be empty). */
  char origin[5];
  char dest[5];
  /** ISO 3166-1 alpha-2 country per side, upper case (may be empty). */
  char origin_cc[3];
  char dest_cc[3];
};

/**
 * Looks up and caches origin/destination routes for callsigns currently
 * seen by services::adsb. Performs at most one network lookup per call and
 * only when its own internal interval is due, so it's safe (and intended)
 * to call this every tick of the background network task. Performs
 * blocking network I/O — do not call from the render loop.
 */
void poll();

/**
 * Route for callsign, or nullptr if the route isn't known yet or genuinely
 * has no schedule data. Only safe to call while holding
 * services::data::lock().
 */
const Route* routeFor(const char* callsign);

}  // namespace services::route
