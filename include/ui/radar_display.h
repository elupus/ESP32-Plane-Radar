#pragma once

namespace ui {

/** Allocate the off-screen frame sprite now. */
bool radarDisplayPreallocate();

/** Draw the static sonar/radar grid (black disc, green overlay, labels). */
void radarDisplayDraw();

/** Redraw aircraft only (blits cached grid; no full-screen clear). */
void radarDisplayRefreshAircraft();

}  // namespace ui
