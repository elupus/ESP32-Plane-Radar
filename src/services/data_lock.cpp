#include "services/data_lock.h"

#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

namespace services::data {

namespace {
SemaphoreHandle_t s_mutex = xSemaphoreCreateMutex();
}  // namespace

void lock() { xSemaphoreTake(s_mutex, portMAX_DELAY); }
void unlock() { xSemaphoreGive(s_mutex); }

}  // namespace services::data
