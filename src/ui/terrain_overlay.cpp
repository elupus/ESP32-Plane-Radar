#include "ui/terrain_overlay.h"

#include <cmath>

#include "config.h"
#include "data/terrain_outlines.h"
#include "services/radar_location.h"
#include "ui/radar_range.h"
#include "ui/radar_theme.h"

namespace ui::terrain {
namespace {

constexpr float kKmPerDeg = 111.0f;
constexpr float kDegToRad = 0.01745329252f;
constexpr float kQMax = 65535.0f;

uint16_t layerColor(uint8_t layer) {
  switch (layer) {
    case data::terrain::kLayerRiver:
      return radar::kColorTerrainRiver;
    case data::terrain::kLayerLake:
      return radar::kColorTerrainLake;
    case data::terrain::kLayerBorder:
      return radar::kColorTerrainBorder;
    default:
      return radar::kColorTerrainCoast;
  }
}

/**
 * Same equirectangular projection as the aircraft and runways, but worked
 * directly in the dataset's quantized units so each point costs two
 * multiply-adds. Coordinates are screen px relative to the radar center.
 */
struct View {
  float center_qx;
  float center_qy;
  float px_per_qx;
  float px_per_qy;
  float min_x;
  float max_x;
  float min_y;
  float max_y;

  float x(const data::terrain::Point& p) const {
    return (p.x - center_qx) * px_per_qx;
  }
  float y(const data::terrain::Point& p) const {
    return -(p.y - center_qy) * px_per_qy;
  }
  bool overlaps(const data::terrain::Chunk& c) const {
    return !(c.max_x < min_x || c.min_x > max_x || c.max_y < min_y ||
             c.min_y > max_y);
  }
  /** The fill only knows land inside the dataset box; elsewhere it's unknown. */
  bool insideDataset() const {
    return min_x >= 0.0f && max_x <= kQMax && min_y >= 0.0f && max_y <= kQMax;
  }
};

View makeView() {
  const float center_lat = static_cast<float>(services::location::lat());
  const float center_lon = static_cast<float>(services::location::lon());
  const float px_per_km =
      static_cast<float>(radar::kGridOuterRadius) / radar::rangeCurrent().outer_km;
  View v;
  v.px_per_qx = data::terrain::kLonSpan / kQMax * kKmPerDeg *
                cosf(center_lat * kDegToRad) * px_per_km;
  v.px_per_qy = data::terrain::kLatSpan / kQMax * kKmPerDeg * px_per_km;
  v.center_qx = (center_lon - data::terrain::kLonMin) / data::terrain::kLonSpan * kQMax;
  v.center_qy = (center_lat - data::terrain::kLatMin) / data::terrain::kLatSpan * kQMax;
  const float r = static_cast<float>(radar::kGridOuterRadius);
  v.min_x = v.center_qx - r / v.px_per_qx;
  v.max_x = v.center_qx + r / v.px_per_qx;
  v.min_y = v.center_qy - r / v.px_per_qy;
  v.max_y = v.center_qy + r / v.px_per_qy;
  return v;
}

int screenX(float x) { return radar::kCenterX + static_cast<int>(lroundf(x)); }
int screenY(float y) { return radar::kCenterY + static_cast<int>(lroundf(y)); }

/** Repaints everything outside the outer ring, where lake fill spilled. */
void clearOutsideRing(lgfx::LGFXBase& gfx) {
  const int r = radar::kGridOuterRadius;
  for (int y = 0; y < radar::kSize; ++y) {
    const int dy = y - radar::kCenterY;
    if (dy < -r || dy > r) {
      gfx.drawFastHLine(0, y, radar::kSize, radar::kColorBackground);
      continue;
    }
    const int half = static_cast<int>(sqrtf(static_cast<float>(r * r - dy * dy)));
    const int left = radar::kCenterX - half;
    const int right = radar::kCenterX + half + 1;
    gfx.drawFastHLine(0, y, left, radar::kColorBackground);
    gfx.drawFastHLine(right, y, radar::kSize - right, radar::kColorBackground);
  }
}

/**
 * Water everywhere inside the ring, land triangles over it in the background
 * colour, then lakes back in water colour. Land spilling past the ring is
 * invisible (same colour as the background); lake spill is cleared after.
 */
void drawFill(lgfx::LGFXBase& gfx, const View& view) {
  gfx.fillCircle(radar::kCenterX, radar::kCenterY, radar::kGridOuterRadius,
                 radar::kColorTerrainWater);
  bool lake_drawn = false;
  for (size_t c = 0; c < data::terrain::kFillChunkCount; ++c) {
    const data::terrain::Chunk& chunk = data::terrain::kFillChunks[c];
    if (!view.overlaps(chunk)) {
      continue;
    }
    const bool lake = chunk.layer == data::terrain::kFillLake;
    lake_drawn |= lake;
    const uint16_t color = lake ? radar::kColorTerrainWater : radar::kColorBackground;
    for (uint8_t i = 0; i < chunk.count; ++i) {
      const data::terrain::Triangle& t = data::terrain::kTriangles[chunk.first + i];
      gfx.fillTriangle(screenX(view.x(t.a)), screenY(view.y(t.a)),
                       screenX(view.x(t.b)), screenY(view.y(t.b)),
                       screenX(view.x(t.c)), screenY(view.y(t.c)), color);
    }
  }
  if (lake_drawn) {
    clearOutsideRing(gfx);
  }
}

/**
 * Clips the segment (x0,y0)-(x1,y1), given relative to the radar center, to
 * the disc of radius r. Returns false when no part of it is inside.
 */
bool clipSegmentToDisc(float* x0, float* y0, float* x1, float* y1, float r) {
  const float dx = *x1 - *x0;
  const float dy = *y1 - *y0;
  const float a = dx * dx + dy * dy;
  const float b = 2.0f * (*x0 * dx + *y0 * dy);
  const float c = *x0 * *x0 + *y0 * *y0 - r * r;
  if (a <= 0.0f) {
    return c <= 0.0f;
  }
  const float disc = b * b - 4.0f * a * c;
  if (disc < 0.0f) {
    return false;
  }
  const float sq = sqrtf(disc);
  const float t_in = std::fmax(0.0f, (-b - sq) / (2.0f * a));
  const float t_out = std::fmin(1.0f, (-b + sq) / (2.0f * a));
  if (t_in > t_out) {
    return false;
  }
  const float sx = *x0;
  const float sy = *y0;
  *x0 = sx + dx * t_in;
  *y0 = sy + dy * t_in;
  *x1 = sx + dx * t_out;
  *y1 = sy + dy * t_out;
  return true;
}

}  // namespace

void drawTerrainOutlines(lgfx::LGFXBase& gfx) {
  if (!config::kShowTerrainOutlines) {
    return;
  }

  const View view = makeView();
  if (view.insideDataset()) {
    drawFill(gfx, view);
  }

  const float r = static_cast<float>(radar::kGridOuterRadius);
  for (size_t c = 0; c < data::terrain::kChunkCount; ++c) {
    const data::terrain::Chunk& chunk = data::terrain::kChunks[c];
    if (!view.overlaps(chunk)) {
      continue;
    }

    const uint16_t color = layerColor(chunk.layer);
    const data::terrain::Point* pts = &data::terrain::kPoints[chunk.first];
    float px = view.x(pts[0]);
    float py = view.y(pts[0]);
    for (uint8_t i = 1; i < chunk.count; ++i) {
      const float nx = view.x(pts[i]);
      const float ny = view.y(pts[i]);
      float x0 = px;
      float y0 = py;
      float x1 = nx;
      float y1 = ny;
      if (clipSegmentToDisc(&x0, &y0, &x1, &y1, r)) {
        gfx.drawLine(screenX(x0), screenY(y0), screenX(x1), screenY(y1), color);
      }
      px = nx;
      py = ny;
    }
  }
}

}  // namespace ui::terrain
