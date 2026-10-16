#include "services/route_lookup.h"

#include <HTTPClient.h>
#include <WiFiClientSecure.h>

#include <ArduinoJson.h>

#include <cctype>
#include <cstdio>
#include <cstring>

#include "services/adsb_client.h"
#include "services/data_lock.h"

namespace services::route {

namespace {

constexpr char kApiBase[] = "https://api.adsbdb.com/v0/callsign/";
constexpr int kConnectAttemptMs = 200;
constexpr unsigned long kRequestTimeoutMs = 8000;
// Offset from adsb's own 3s poll so the two never contend for the single
// network op the background task can do at a time, and so a bad lookup
// can't compound with an in-flight aircraft fetch.
constexpr unsigned long kLookupIntervalMs = 4000;

struct CacheEntry {
  char callsign[9] = {0};
  Route route = {};    // route.text empty = no usable route
  bool known = false;  // true once this callsign has a definitive answer
  bool in_use = false;
  unsigned long last_seen_ms = 0;
};

constexpr size_t kCacheSize = services::adsb::kMaxAircraft;
CacheEntry s_cache[kCacheSize];

unsigned long s_last_lookup_ms = 0;

// Mirrors adsb_client's PollingStreamReader: feeds ArduinoJson directly from
// the stream instead of buffering the response into a String first. The
// route response is tiny, but this keeps the same low-heap-pressure shape
// as the aircraft fetch. Runs on the background network task, so the plain
// delay() below yields to the render task.
class PollingStreamReader {
 public:
  PollingStreamReader(WiFiClient* stream, unsigned long deadline)
      : stream_(stream), deadline_(deadline) {}

  int read() {
    char c;
    return readBytes(&c, 1) == 1 ? static_cast<uint8_t>(c) : -1;
  }

  size_t readBytes(char* buffer, size_t length) {
    while (millis() < deadline_) {
      const int available = stream_->available();
      if (available > 0) {
        const size_t to_read =
            length < static_cast<size_t>(available) ? length
                                                     : static_cast<size_t>(available);
        const int read_bytes = stream_->readBytes(buffer, to_read);
        if (read_bytes > 0) {
          bytes_read_ += static_cast<size_t>(read_bytes);
          return static_cast<size_t>(read_bytes);
        }
      }
      if (!stream_->connected() && stream_->available() <= 0) {
        return 0;
      }
      delay(1);
    }
    return 0;
  }

  size_t bytesRead() const { return bytes_read_; }

  // See adsb_client's PollingStreamReader::drainTo: consumes any
  // Content-Length bytes ArduinoJson didn't need, so the connection closes
  // clean instead of HTTPClient logging "still data in buffer". Anything
  // beyond RFC 8259 insignificant whitespace here would mean we stopped
  // parsing too early, so flag it instead of silently discarding it.
  void drainTo(size_t total_len) {
    char discard[64];
    while (bytes_read_ < total_len) {
      const size_t remaining = total_len - bytes_read_;
      const size_t chunk = remaining < sizeof(discard) ? remaining : sizeof(discard);
      const size_t got = readBytes(discard, chunk);
      if (got == 0) {
        break;  // timed out or the stream closed early
      }
      for (size_t i = 0; i < got; ++i) {
        const char c = discard[i];
        if (c != ' ' && c != '\t' && c != '\r' && c != '\n') {
          Serial.printf(
              "route: unexpected byte 0x%02x after JSON document (parsed "
              "too early?)\n",
              static_cast<unsigned char>(c));
        }
      }
    }
  }

 private:
  WiFiClient* stream_;
  unsigned long deadline_;
  size_t bytes_read_ = 0;
};

CacheEntry* findEntry(const char* callsign) {
  for (CacheEntry& e : s_cache) {
    if (e.in_use && strncmp(e.callsign, callsign, sizeof(e.callsign)) == 0) {
      return &e;
    }
  }
  return nullptr;
}

// Claims the least-recently-seen slot (or an empty one) for a new callsign.
CacheEntry* claimSlotFor(const char* callsign) {
  CacheEntry* victim = &s_cache[0];
  for (CacheEntry& e : s_cache) {
    if (!e.in_use) {
      victim = &e;
      break;
    }
    if (e.last_seen_ms < victim->last_seen_ms) {
      victim = &e;
    }
  }
  victim->in_use = true;
  victim->known = false;
  victim->route = Route{};
  strncpy(victim->callsign, callsign, sizeof(victim->callsign) - 1);
  victim->callsign[sizeof(victim->callsign) - 1] = '\0';
  return victim;
}

void copyString(char* out, size_t out_len, const char* s) {
  strncpy(out, s, out_len - 1);
  out[out_len - 1] = '\0';
}

void readAirport(JsonVariantConst airport, char* code, size_t code_len, char* cc,
                 size_t cc_len) {
  code[0] = '\0';
  cc[0] = '\0';
  if (airport["iata_code"].is<const char*>() &&
      airport["iata_code"].as<const char*>()[0] != '\0') {
    copyString(code, code_len, airport["iata_code"].as<const char*>());
  } else if (airport["icao_code"].is<const char*>()) {
    copyString(code, code_len, airport["icao_code"].as<const char*>());
  }
  if (airport["country_iso_name"].is<const char*>()) {
    const char* iso = airport["country_iso_name"].as<const char*>();
    if (strlen(iso) == 2) {
      cc[0] = static_cast<char>(toupper(static_cast<unsigned char>(iso[0])));
      cc[1] = static_cast<char>(toupper(static_cast<unsigned char>(iso[1])));
      cc[2] = '\0';
    }
  }
}

void performLookup(CacheEntry* entry) {
  WiFiClientSecure client;
  client.setInsecure();

  HTTPClient http;
  String url = kApiBase;
  url += entry->callsign;
  if (!http.begin(client, url)) {
    services::data::lock();
    entry->known = true;  // give up for this session; don't retry every cycle
    services::data::unlock();
    return;
  }
  // HTTP/1.0 so the server can't use chunked transfer encoding: we hand the
  // raw socket to ArduinoJson, which would choke on the chunk-size lines.
  http.useHTTP10(true);
  http.setReuse(false);
  http.setConnectTimeout(kConnectAttemptMs);
  http.setTimeout(kRequestTimeoutMs);
  const unsigned long deadline = millis() + kRequestTimeoutMs;
  int code = 0;
  while (millis() < deadline) {
    code = http.GET();
    if (code > 0 || (code != HTTPC_ERROR_CONNECTION_REFUSED &&
                     code != HTTPC_ERROR_NOT_CONNECTED)) {
      break;
    }
    delay(5);
  }

  services::data::lock();
  entry->known = true;  // any outcome from here is definitive for this session
  services::data::unlock();
  if (code != HTTP_CODE_OK) {
    http.end();
    return;  // 404 = unknown callsign, or a transient failure — either way, no route
  }

  WiFiClient* stream = http.getStreamPtr();
  if (stream == nullptr) {
    http.end();
    return;
  }

  PollingStreamReader reader(stream, millis() + kRequestTimeoutMs);
  JsonDocument doc;
  const DeserializationError err = deserializeJson(doc, reader);
  const int content_len = http.getSize();
  if (content_len > 0) {
    reader.drainTo(static_cast<size_t>(content_len));
  }
  http.end();
  if (err) {
    return;
  }

  JsonVariantConst flightroute = doc["response"]["flightroute"];
  if (flightroute.isNull()) {
    return;
  }

  Route route = {};
  readAirport(flightroute["origin"], route.origin, sizeof(route.origin),
              route.origin_cc, sizeof(route.origin_cc));
  readAirport(flightroute["destination"], route.dest, sizeof(route.dest),
              route.dest_cc, sizeof(route.dest_cc));
  if (route.origin[0] != '\0' || route.dest[0] != '\0') {
    snprintf(route.text, sizeof(route.text), "%s>%s", route.origin, route.dest);
  }

  services::data::lock();
  entry->route = route;
  services::data::unlock();
}

}  // namespace

void poll() {
  const unsigned long now = millis();
  if (now - s_last_lookup_ms < kLookupIntervalMs) {
    return;
  }

  const services::adsb::Aircraft* aircraft = services::adsb::aircraftList();
  const size_t count = services::adsb::aircraftCount();
  for (size_t i = 0; i < count; ++i) {
    if (aircraft[i].callsign[0] == '\0') {
      continue;
    }
    CacheEntry* entry = findEntry(aircraft[i].callsign);
    if (entry != nullptr) {
      entry->last_seen_ms = now;
      continue;
    }
    services::data::lock();
    entry = claimSlotFor(aircraft[i].callsign);
    entry->last_seen_ms = now;
    services::data::unlock();
    s_last_lookup_ms = now;
    performLookup(entry);
    return;  // one lookup per call, regardless of outcome
  }
}

const Route* routeFor(const char* callsign) {
  if (callsign == nullptr || callsign[0] == '\0') {
    return nullptr;
  }
  CacheEntry* entry = findEntry(callsign);
  if (entry == nullptr || !entry->known || entry->route.text[0] == '\0') {
    return nullptr;
  }
  return &entry->route;
}

}  // namespace services::route
