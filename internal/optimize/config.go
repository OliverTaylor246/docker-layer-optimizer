package optimize

import (
	"fmt"
	"os"
	"path/filepath"

	"gopkg.in/yaml.v3"
)

type SettingsOverrides struct {
	Trials                                                                                                                         *int
	BudgetSeconds, MinRelativeImprovement, MinAbsoluteSeconds, MaxRelativeRegression, MaxAbsoluteRegressionSeconds, PaybackDeploys *float64
	SourcePath                                                                                                                     *string
	VerificationCommands                                                                                                           []string
	Platform, Target, Builder                                                                                                      *string
	BuildArgs                                                                                                                      []string
}

type configFile struct {
	Version   int `yaml:"version"`
	Benchmark struct {
		Trials                       *int     `yaml:"trials"`
		BudgetSeconds                *float64 `yaml:"budget_seconds"`
		MinRelativeImprovement       *float64 `yaml:"min_relative_improvement"`
		MinAbsoluteSeconds           *float64 `yaml:"min_absolute_seconds"`
		MaxRelativeRegression        *float64 `yaml:"max_relative_regression"`
		MaxAbsoluteRegressionSeconds *float64 `yaml:"max_absolute_regression_seconds"`
		PaybackDeploys               *float64 `yaml:"payback_deploys"`
		SourcePath                   string   `yaml:"source_path"`
	} `yaml:"benchmark"`
	Verification struct {
		Commands []string `yaml:"commands"`
	} `yaml:"verification"`
}

func LoadSettings(root string, overrides SettingsOverrides) (Settings, error) {
	settings := DefaultSettings()
	path := filepath.Join(root, ".dlo.yml")
	if data, err := os.ReadFile(path); err == nil {
		var config configFile
		if err := yaml.Unmarshal(data, &config); err != nil {
			return Settings{}, fmt.Errorf("invalid .dlo.yml: %w", err)
		}
		if config.Version != 0 && config.Version != 1 {
			return Settings{}, fmt.Errorf(".dlo.yml must be a mapping with version: 1")
		}
		if config.Benchmark.Trials != nil {
			settings.Trials = *config.Benchmark.Trials
		}
		if config.Benchmark.BudgetSeconds != nil {
			settings.BudgetSeconds = *config.Benchmark.BudgetSeconds
		}
		if config.Benchmark.MinRelativeImprovement != nil {
			settings.MinRelativeImprovement = *config.Benchmark.MinRelativeImprovement
		}
		if config.Benchmark.MinAbsoluteSeconds != nil {
			settings.MinAbsoluteSeconds = *config.Benchmark.MinAbsoluteSeconds
		}
		if config.Benchmark.MaxRelativeRegression != nil {
			settings.MaxRelativeRegression = *config.Benchmark.MaxRelativeRegression
		}
		if config.Benchmark.MaxAbsoluteRegressionSeconds != nil {
			settings.MaxAbsoluteRegressionSeconds = *config.Benchmark.MaxAbsoluteRegressionSeconds
		}
		if config.Benchmark.PaybackDeploys != nil {
			settings.PaybackDeploys = *config.Benchmark.PaybackDeploys
		}
		settings.SourcePath = config.Benchmark.SourcePath
		settings.VerificationCommands = append(settings.VerificationCommands, config.Verification.Commands...)
	} else if !os.IsNotExist(err) {
		return Settings{}, err
	}
	if overrides.Trials != nil {
		settings.Trials = *overrides.Trials
	}
	if overrides.BudgetSeconds != nil {
		settings.BudgetSeconds = *overrides.BudgetSeconds
	}
	if overrides.MinRelativeImprovement != nil {
		settings.MinRelativeImprovement = *overrides.MinRelativeImprovement
	}
	if overrides.MinAbsoluteSeconds != nil {
		settings.MinAbsoluteSeconds = *overrides.MinAbsoluteSeconds
	}
	if overrides.MaxRelativeRegression != nil {
		settings.MaxRelativeRegression = *overrides.MaxRelativeRegression
	}
	if overrides.MaxAbsoluteRegressionSeconds != nil {
		settings.MaxAbsoluteRegressionSeconds = *overrides.MaxAbsoluteRegressionSeconds
	}
	if overrides.PaybackDeploys != nil {
		settings.PaybackDeploys = *overrides.PaybackDeploys
	}
	if overrides.SourcePath != nil {
		settings.SourcePath = *overrides.SourcePath
	}
	settings.VerificationCommands = append(settings.VerificationCommands, overrides.VerificationCommands...)
	if overrides.Platform != nil {
		settings.Platform = *overrides.Platform
	}
	if overrides.Target != nil {
		settings.Target = *overrides.Target
	}
	if overrides.Builder != nil {
		settings.Builder = *overrides.Builder
	}
	settings.BuildArgs = append([]string(nil), overrides.BuildArgs...)
	if settings.Trials < 3 {
		return Settings{}, fmt.Errorf("optimization requires at least three paired trials")
	}
	if settings.BudgetSeconds <= 0 {
		return Settings{}, fmt.Errorf("optimization budget must be positive")
	}
	if settings.MinRelativeImprovement <= 0 || settings.MinRelativeImprovement >= 1 {
		return Settings{}, fmt.Errorf("minimum relative improvement must be between 0 and 1")
	}
	if settings.MinAbsoluteSeconds < 0 {
		return Settings{}, fmt.Errorf("minimum absolute improvement cannot be negative")
	}
	if settings.MaxRelativeRegression < 0 || settings.MaxRelativeRegression >= 1 {
		return Settings{}, fmt.Errorf("maximum relative regression must be between 0 and 1")
	}
	if settings.MaxAbsoluteRegressionSeconds < 0 {
		return Settings{}, fmt.Errorf("maximum absolute regression cannot be negative")
	}
	if settings.PaybackDeploys <= 0 {
		return Settings{}, fmt.Errorf("payback deployment limit must be positive")
	}
	return settings, nil
}
