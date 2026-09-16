#pragma once

#include <stdexcept>
#include <string>
#include <vector>

// Shared across systemc/models/*.{h,cpp} the same way
// src/ip_model_automation/common.py's WeightedOrder is shared across the
// SimPy models -- ported here rather than re-derived so both backends pick
// the same tenant/port/SQ every time given the same weights and history.
class WeightedOrder {
  public:
    explicit WeightedOrder(const std::vector<std::pair<std::string, int>>& weights) {
        for (const auto& [name, weight] : weights) {
            for (int i = 0; i < weight; i++) {
                order.push_back(name);
            }
        }
        if (order.empty()) {
            throw std::invalid_argument("at least one weight must be positive");
        }
    }

    // A full lap starting at the current pointer, materialized eagerly (the
    // Python original yields lazily; a SystemC caller always consumes the
    // whole scan before advancing, so eager is equivalent and simpler).
    std::vector<std::string> scan() const {
        std::vector<std::string> result;
        result.reserve(order.size());
        for (size_t offset = 0; offset < order.size(); offset++) {
            result.push_back(order[(index + offset) % order.size()]);
        }
        return result;
    }

    void advance_to_after(const std::string& selected) {
        for (size_t offset = 0; offset < order.size(); offset++) {
            size_t idx = (index + offset) % order.size();
            if (order[idx] == selected) {
                index = idx + 1;
                return;
            }
        }
    }

  private:
    std::vector<std::string> order;
    size_t index = 0;
};
