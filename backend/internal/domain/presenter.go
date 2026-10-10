package domain

import (
	"fmt"
	"math"
	"regexp"
)

// PresenterEffects selects a server-owned Cubism model for final-video compositing.
type PresenterEffects struct {
	AssetID        string  `json:"asset_id"`
	ModelPath      string  `json:"model_path,omitempty"` // resolved only by the API
	MouthParameter string  `json:"mouth_parameter,omitempty"`
	MouthGain      float64 `json:"mouth_gain,omitempty"`
}

// Validate rejects invalid controls before a costly render is queued.
func (p *PresenterEffects) Validate() error {
	if p == nil {
		return nil
	}
	if !regexp.MustCompile(`^[0-9a-f]{16,64}$`).MatchString(p.AssetID) {
		return fmt.Errorf("请选择已导入的 Live2D 模型")
	}
	if p.MouthParameter != "" && !regexp.MustCompile(`^[A-Za-z][A-Za-z0-9_]{0,63}$`).MatchString(p.MouthParameter) {
		return fmt.Errorf("口型参数名无效")
	}
	if math.IsNaN(p.MouthGain) || math.IsInf(p.MouthGain, 0) || p.MouthGain < 0 || p.MouthGain > 3 {
		return fmt.Errorf("口型强度须在 0～3 之间（0 使用默认值 1）")
	}
	return nil
}
