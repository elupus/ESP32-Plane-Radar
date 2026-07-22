#pragma once

namespace services::data {

/**
 * Guards route (services::route) state shared between the background network
 * task and the render loop. Aircraft have their own lock inside
 * services::adsb (see snapshotAircraft()). The network task holds this only
 * for its brief in-memory commit of route results; the renderer holds it
 * while drawing tags so a route string is never read mid-update.
 */
void lock();
void unlock();

}  // namespace services::data
