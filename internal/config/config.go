package config

import (
	"fmt"
	"os"
)

// Config 保存所有服务配置，优先读取环境变量
type Config struct {
	RabbitMQ RabbitMQConfig
	Redis    RedisConfig
	HTTP     HTTPConfig
}

type RabbitMQConfig struct {
	URL string // amqp://user:pass@host:port/
}

type RedisConfig struct {
	Addr     string
	Password string
	DB       int
}

type HTTPConfig struct {
	Addr string // ":8080"
}

func Load() (*Config, error) {
	cfg := &Config{
		RabbitMQ: RabbitMQConfig{
			URL: getEnv("RABBITMQ_URL", "amqp://closeclaw:closeclaw@localhost:5672/"),
		},
		Redis: RedisConfig{
			Addr:     getEnv("REDIS_ADDR", "localhost:6379"),
			Password: getEnv("REDIS_PASSWORD", ""),
		},
		HTTP: HTTPConfig{
			Addr: getEnv("HTTP_ADDR", ":8080"),
		},
	}

	if cfg.RabbitMQ.URL == "" {
		return nil, fmt.Errorf("RABBITMQ_URL is required")
	}

	return cfg, nil
}

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
