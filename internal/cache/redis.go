package cache

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/redis/go-redis/v9"
)

const (
	jobKeyPrefix     = "closeclaw:job:"
	triggerKeyPrefix = "closeclaw:trigger:"
	jobTTL           = 48 * time.Hour
)

// Client 封装 Redis 操作，用于持久化 Job 状态
type Client struct {
	rdb *redis.Client
}

// New 建立 Redis 连接
func New(addr, password string, db int) (*Client, error) {
	rdb := redis.NewClient(&redis.Options{
		Addr:     addr,
		Password: password,
		DB:       db,
	})

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	if err := rdb.Ping(ctx).Err(); err != nil {
		return nil, fmt.Errorf("redis ping: %w", err)
	}

	return &Client{rdb: rdb}, nil
}

// SaveJob 将 Job 序列化后存入 Redis
func (c *Client) SaveJob(ctx context.Context, job any) error {
	type withID interface{ GetID() string }

	b, err := json.Marshal(job)
	if err != nil {
		return fmt.Errorf("marshal job: %w", err)
	}

	// job 需要提供 ID 字段，这里通过反射 JSON 解析获取
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(b, &raw); err != nil {
		return fmt.Errorf("unmarshal job id: %w", err)
	}
	var id string
	if err := json.Unmarshal(raw["id"], &id); err != nil {
		return fmt.Errorf("get job id: %w", err)
	}

	key := jobKeyPrefix + id
	if err := c.rdb.Set(ctx, key, b, jobTTL).Err(); err != nil {
		return err
	}

	var triggerJobID string
	if rawTrigger, ok := raw["trigger_job_id"]; ok {
		_ = json.Unmarshal(rawTrigger, &triggerJobID)
	}
	if triggerJobID != "" {
		triggerKey := triggerKeyPrefix + triggerJobID + ":jobs"
		if err := c.rdb.SAdd(ctx, triggerKey, id).Err(); err != nil {
			return err
		}
		if err := c.rdb.Expire(ctx, triggerKey, jobTTL).Err(); err != nil {
			return err
		}
	}

	return nil
}

// GetJob 从 Redis 取回 Job JSON
func (c *Client) GetJob(ctx context.Context, id string) ([]byte, error) {
	b, err := c.rdb.Get(ctx, jobKeyPrefix+id).Bytes()
	if err == redis.Nil {
		return nil, fmt.Errorf("job %q not found", id)
	}
	return b, err
}

// SetStage 更新 Job 的 stage 字段（原子操作：先 GET 再 SET）
func (c *Client) SetStage(ctx context.Context, id, stage, status string) error {
	b, err := c.GetJob(ctx, id)
	if err != nil {
		return err
	}

	var raw map[string]json.RawMessage
	if err := json.Unmarshal(b, &raw); err != nil {
		return err
	}

	raw["stage"], _ = json.Marshal(stage)
	raw["status"], _ = json.Marshal(status)
	raw["updated_at"], _ = json.Marshal(time.Now().UTC().Format(time.RFC3339))

	updated, err := json.Marshal(raw)
	if err != nil {
		return err
	}

	return c.rdb.Set(ctx, jobKeyPrefix+id, updated, jobTTL).Err()
}

// ListJobs 返回所有 Job ID
func (c *Client) ListJobs(ctx context.Context) ([]string, error) {
	keys, err := c.rdb.Keys(ctx, jobKeyPrefix+"*").Result()
	if err != nil {
		return nil, err
	}
	ids := make([]string, len(keys))
	for i, k := range keys {
		ids[i] = k[len(jobKeyPrefix):]
	}
	return ids, nil
}

// ListJobsByTriggerJobID 返回同一 trigger 下的所有 Job JSON。
func (c *Client) ListJobsByTriggerJobID(ctx context.Context, triggerJobID string) ([][]byte, error) {
	if triggerJobID == "" {
		return nil, nil
	}

	triggerKey := triggerKeyPrefix + triggerJobID + ":jobs"
	ids, err := c.rdb.SMembers(ctx, triggerKey).Result()
	if err != nil {
		return nil, err
	}

	if len(ids) == 0 {
		return c.scanJobsByTriggerJobID(ctx, triggerJobID)
	}

	jobs := make([][]byte, 0, len(ids))
	for _, id := range ids {
		b, err := c.GetJob(ctx, id)
		if err != nil {
			continue
		}
		jobs = append(jobs, b)
	}

	if len(jobs) == 0 {
		return c.scanJobsByTriggerJobID(ctx, triggerJobID)
	}
	return jobs, nil
}

func (c *Client) scanJobsByTriggerJobID(ctx context.Context, triggerJobID string) ([][]byte, error) {
	ids, err := c.ListJobs(ctx)
	if err != nil {
		return nil, err
	}

	jobs := make([][]byte, 0)
	for _, id := range ids {
		b, err := c.GetJob(ctx, id)
		if err != nil {
			continue
		}

		var raw map[string]json.RawMessage
		if err := json.Unmarshal(b, &raw); err != nil {
			continue
		}

		var currentTrigger string
		if rawTrigger, ok := raw["trigger_job_id"]; ok {
			_ = json.Unmarshal(rawTrigger, &currentTrigger)
		}
		if currentTrigger == triggerJobID {
			jobs = append(jobs, b)
		}
	}
	return jobs, nil
}

// Close 关闭连接
func (c *Client) Close() error {
	return c.rdb.Close()
}
