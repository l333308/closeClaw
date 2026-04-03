package queue

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	amqp "github.com/rabbitmq/amqp091-go"
)

// QueueName 是各阶段对应的 RabbitMQ 队列名
const (
	QueueCrawl   = "closeclaw.crawl"
	QueueDedup   = "closeclaw.dedup"
	QueueAnalyze = "closeclaw.analyze"
	QueueWrite   = "closeclaw.write"
	QueueVideo   = "closeclaw.video"
	QueuePublish = "closeclaw.publish"
)

// AllQueues 按执行顺序列出所有队列
var AllQueues = []string{
	QueueCrawl, QueueDedup, QueueAnalyze, QueueWrite, QueueVideo, QueuePublish,
}

// Client 封装 RabbitMQ 连接与 channel
type Client struct {
	conn    *amqp.Connection
	channel *amqp.Channel
}

// New 建立连接并声明所有队列
func New(url string) (*Client, error) {
	conn, err := amqp.Dial(url)
	if err != nil {
		return nil, fmt.Errorf("rabbitmq dial: %w", err)
	}

	ch, err := conn.Channel()
	if err != nil {
		conn.Close()
		return nil, fmt.Errorf("rabbitmq channel: %w", err)
	}

	c := &Client{conn: conn, channel: ch}
	if err := c.declareQueues(); err != nil {
		c.Close()
		return nil, err
	}

	slog.Info("rabbitmq connected", "url", url)
	return c, nil
}

func (c *Client) declareQueues() error {
	for _, name := range AllQueues {
		_, err := c.channel.QueueDeclare(
			name,
			true,  // durable
			false, // autoDelete
			false, // exclusive
			false, // noWait
			nil,
		)
		if err != nil {
			return fmt.Errorf("declare queue %q: %w", name, err)
		}
	}
	return nil
}

// Publish 将消息发送到指定队列
func (c *Client) Publish(ctx context.Context, queue string, body []byte) error {
	return c.channel.PublishWithContext(
		ctx,
		"",    // exchange（使用默认）
		queue, // routing key
		false, // mandatory
		false, // immediate
		amqp.Publishing{
			ContentType:  "application/json",
			DeliveryMode: amqp.Persistent,
			Timestamp:    time.Now(),
			Body:         body,
		},
	)
}

// Consume 开始消费指定队列，返回消息 channel
func (c *Client) Consume(queue string) (<-chan amqp.Delivery, error) {
	return c.channel.Consume(
		queue,
		"",    // consumer tag（自动生成）
		false, // autoAck（手动 ack）
		false, // exclusive
		false, // noLocal
		false, // noWait
		nil,
	)
}

// Close 关闭连接
func (c *Client) Close() {
	if c.channel != nil {
		c.channel.Close()
	}
	if c.conn != nil {
		c.conn.Close()
	}
}
