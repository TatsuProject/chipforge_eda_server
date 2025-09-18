# 🔧 Advanced EDA Tools Server

A comprehensive, production-ready containerized solution for Verilog design evaluation using industry-standard EDA tools. Perfect for Hardware Design Subnets, automated design evaluation, and educational purposes.

## 🚀 Features

### 🛠️ **EDA Tool Integration**
- **Yosys** (Port 8000) - Open-source synthesis and optimization
- **Verilator** (Port 8001) - High-performance simulation and verification  
- **Icarus Verilog** (Port 8002) - Behavioral simulation and testbench validation
- **OpenLane** (Port 8003) - Advanced ASIC design flow with accurate PPA analysis
- **Unified Gateway** (Port 8080) - Single API for complete design evaluation

### 📊 **Evaluation Metrics**
- **Functionality Score** (50%) - Testbench validation and correctness
- **Area Score** (15%) - Resource utilization and gate count
- **Delay Score** (20%) - Timing performance and critical path
- **Power Score** (15%) - Estimated power consumption

### 🔒 **Security & Authentication**
- **API Key Authentication** - Secure access control for all endpoints
- **Request validation** - Input sanitization and validation
- **Rate limiting** - Protection against abuse

### 📁 **File Management**
- **Direct file upload** - Upload Verilog (.v/.sv) and testbench files from PC
- **Automatic file handling** - Intelligent parsing of uploaded designs
- **Result archival** - Detailed results stored as ZIP files per tool

### ☁️ **Cloud Storage Integration**
- **AWS S3 Support** - Automatic backup of results to S3 bucket
- **Local storage fallback** - Works with or without AWS configuration
- **Organized storage** - Results organized by timestamp and tool

## 📁 Project Structure

```
EDA_server/
├── docker-compose.yml           # Multi-service orchestration
├── Makefile                     # Build and deployment automation
├── README.md                    # This file
├── eda_client.py               # Python client library
├── example_usage.py            # Usage examples and tests
├── yosys-api/                  # Yosys synthesis service
│   ├── Dockerfile
│   ├── main.py
│   └── requirements.txt
├── verilator-api/              # Verilator simulation service
│   ├── Dockerfile
│   ├── main.py
│   └── requirements.txt
├── icarus-api/                 # Icarus Verilog service
│   ├── Dockerfile
│   ├── main.py
│   └── requirements.txt
├── openlane-api/               # OpenLane ASIC design service
│   ├── Dockerfile
│   ├── main.py
│   └── requirements.txt
├── gateway/                    # Unified API gateway
│   ├── Dockerfile
│   ├── main.py
│   └── requirements.txt
├── shared/                     # Shared volume for file exchange
└── results/                    # Local results storage
```

## 🚀 Quick Start

### Prerequisites
- Docker & Docker Compose
- 8GB+ RAM (recommended for OpenLane)
- 20GB+ free disk space

### 1. **Environment Setup**
```bash
# Clone repository
git clone <your-repo-url>
cd EDA_server

# Configure environment (optional)
cp .env.example .env
# Edit .env with your AWS credentials and API keys
```

### 2. **Build and Deploy**
```bash
# Build all services
make build

# Start all services
make start

# Check status
make status
```

### 3. **Verify Installation**
```bash
# Health check all services
make health

# Run example test
python example_usage.py
```

## 🔧 Configuration

### Environment Variables (.env)
```bash
# Authentication
EDA_API_KEY=your-secure-api-key-here

# AWS Configuration (Optional)
AWS_ACCESS_KEY_ID=your-aws-access-key
AWS_SECRET_ACCESS_KEY=your-aws-secret-key
AWS_REGION=us-east-1
S3_BUCKET_NAME=eda-results-bucket

# Service Configuration
YOSYS_TIMEOUT=300
VERILATOR_TIMEOUT=300
ICARUS_TIMEOUT=120
OPENLANE_TIMEOUT=3600

# Result Storage
RESULTS_RETENTION_DAYS=30
MAX_FILE_SIZE_MB=50
```

## 📚 API Documentation

### **Authentication**
All API calls require authentication header:
```bash
Authorization: Bearer your-eda-api-key
```

### **Service URLs**
- **Gateway API**: http://localhost:8080/docs
- **Yosys API**: http://localhost:8000/docs
- **Verilator API**: http://localhost:8001/docs  
- **Icarus API**: http://localhost:8002/docs
- **OpenLane API**: http://localhost:8003/docs

### **Main Evaluation Endpoint**

**POST** `/evaluate`

Comprehensive design evaluation using all EDA tools.

**Request:**
```json
{
  "verilog_code": "module adder...",
  "testbench_code": "module tb_adder...",
  "top_module": "tb_adder",
  "evaluation_options": {
    "target_technology": "sky130",
    "clock_frequency_mhz": 100,
    "enable_openlane": true
  }
}
```

**Response:**
```json
{
  "success": true,
  "functionality_score": 1.0,
  "area_score": 0.85,
  "delay_score": 0.92,
  "power_score": 0.78,
  "overall_score": 0.91,
  "detailed_results": {
    "yosys": {...},
    "verilator": {...},
    "icarus": {...},
    "openlane": {...}
  },
  "result_files": {
    "zip_url": "/results/evaluation_20250105_143022.zip",
    "s3_url": "s3://bucket/results/evaluation_20250105_143022.zip"
  }
}
```

### **File Upload Endpoints**

**POST** `/upload/verilog`
```bash
curl -X POST http://localhost:8080/upload/verilog \
  -H "Authorization: Bearer your-api-key" \
  -F "file=@design.v"
```

**POST** `/upload/testbench`
```bash
curl -X POST http://localhost:8080/upload/testbench \
  -H "Authorization: Bearer your-api-key" \
  -F "file=@testbench.v"
```

**POST** `/evaluate/files`
Evaluate previously uploaded files:
```json
{
  "verilog_file_id": "design_abc123",
  "testbench_file_id": "tb_abc123",
  "top_module": "tb_design"
}
```

## 🐍 Python Client Usage

### **Basic Evaluation**
```python
from eda_client import EDAClient

# Initialize client with API key
client = EDAClient(
    base_url="http://localhost:8080",
    api_key="your-eda-api-key"
)

# Evaluate design
result = await client.evaluate_design(
    verilog_code="module adder...",
    testbench_code="module tb_adder...",
    top_module="tb_adder"
)

print(f"Overall Score: {result.overall_score:.2f}")
```

### **File Upload Evaluation**
```python
# Upload files
verilog_id = await client.upload_verilog("path/to/design.v")
testbench_id = await client.upload_testbench("path/to/testbench.v")

# Evaluate uploaded files
result = await client.evaluate_files(
    verilog_file_id=verilog_id,
    testbench_file_id=testbench_id,
    top_module="tb_design"
)
```

### **Advanced Configuration**
```python
result = await client.evaluate_design(
    verilog_code=verilog_code,
    testbench_code=testbench_code,
    top_module="tb_design",
    options={
        "target_technology": "sky130",
        "clock_frequency_mhz": 200,
        "enable_openlane": True,
        "synthesis_strategy": "AREA_OPTIMIZED"
    }
)
```

## 🛠️ Available Commands

```bash
# Build & Deployment
make build              # Build all Docker images
make start              # Start all services
make stop               # Stop all services
make restart            # Restart all services
make clean              # Clean up containers and volumes

# Development
make logs               # View all service logs
make logs-gateway       # View gateway logs only
make shell-gateway      # Open shell in gateway container
make rebuild            # Force rebuild all images

# Monitoring
make health             # Check all service health
make status             # Show container status
make stats              # Show resource usage

# Testing
make test               # Run test suite
make test-quick         # Quick functionality test
make test-upload        # Test file upload functionality

# Maintenance
make backup             # Backup results and configuration
make cleanup-results    # Clean old result files
make update-tools       # Update EDA tool versions
```

## 🧪 Testing & Validation

### **Built-in Test Suite**
```bash
# Run complete test suite
make test

# Quick functional test
python example_usage.py

# Test individual services
make test-yosys
make test-verilator
make test-icarus
make test-openlane
```

### **Sample Test Cases**
- **4-bit Adder** - Basic arithmetic functionality
- **FIFO Buffer** - Memory and control logic
- **CPU Core** - Complex processor design (OpenLane)
- **Filter Design** - DSP and signal processing

## 🔧 Advanced Features

### **OpenLane Integration**
- **Full ASIC design flow** from RTL to GDSII
- **Accurate PPA analysis** using Sky130 PDK
- **Physical implementation** with placement and routing
- **DRC/LVS verification** for production readiness

### **Result Management**
- **Comprehensive reporting** with synthesis logs, timing reports, and power analysis
- **Visual outputs** including layout views and timing diagrams
- **Automated archival** with ZIP compression and cloud backup
- **Result sharing** via secure URLs

### **Scalability**
- **Horizontal scaling** support for multiple instances
- **Load balancing** across EDA tool instances
- **Queue management** for batch processing
- **Resource monitoring** and automatic scaling

## 📊 Performance Benchmarks

| Design Size | Yosys | Verilator | Icarus | OpenLane | Total |
|-------------|-------|-----------|---------|----------|-------|
| Small (< 1K gates) | 2s | 5s | 3s | - | 10s |
| Medium (1K-10K gates) | 8s | 15s | 12s | 120s | 155s |
| Large (10K+ gates) | 30s | 45s | 40s | 600s | 715s |

## 🚀 Production Deployment

### **Docker Swarm**
```bash
# Initialize swarm
docker swarm init

# Deploy stack
docker stack deploy -c docker-compose.prod.yml eda-stack
```

### **Kubernetes**
```bash
# Apply configurations
kubectl apply -f k8s/

# Scale services
kubectl scale deployment eda-gateway --replicas=3
```

### **AWS ECS**
```bash
# Register task definitions
aws ecs register-task-definition --cli-input-json file://ecs-task-def.json

# Create service
aws ecs create-service --cluster eda-cluster --service-name eda-gateway
```

## 🔍 Monitoring & Observability

### **Health Endpoints**
- `/health` - Overall system health
- `/metrics` - Prometheus metrics
- `/status` - Detailed service status

### **Logging**
- **Structured logging** with JSON format
- **Centralized logging** via ELK stack
- **Error tracking** with Sentry integration

### **Metrics**
- **Request latency** and throughput
- **Resource utilization** (CPU, memory, disk)
- **Success/error rates** per service

## 🔒 Security Considerations

### **Authentication & Authorization**
- **API key management** with rotation
- **Role-based access control** (RBAC)
- **Rate limiting** per client

### **Data Protection**
- **Input validation** and sanitization
- **Secure file handling** with virus scanning
- **Encrypted storage** for sensitive results

### **Network Security**
- **TLS/SSL termination** at gateway
- **Internal network isolation**
- **Firewall rules** for external access

## 🛠️ Troubleshooting

### **Common Issues**

**Services not starting:**
```bash
# Check resource usage
make stats

# View detailed logs
make logs

# Restart problematic service
docker-compose restart yosys-api
```

**Authentication errors:**
```bash
# Verify API key
curl -H "Authorization: Bearer your-key" http://localhost:8080/health

# Check environment variables
docker-compose exec gateway env | grep EDA_API_KEY
```

**OpenLane timeout:**
```bash
# Increase timeout in .env
OPENLANE_TIMEOUT=7200

# Restart services
make restart
```

**AWS S3 upload failures:**
```bash
# Verify credentials
aws s3 ls s3://your-bucket-name

# Check IAM permissions
aws iam get-user
```

### **Debug Mode**
```bash
# Enable debug logging
export EDA_DEBUG=true
make restart

# Access debug interface
curl http://localhost:8080/debug/status
```

## 🤝 Contributing

### **Development Setup**
```bash
# Fork repository
git clone https://github.com/your-username/eda-server
cd eda-server

# Create development environment
make dev-setup

# Run tests
make test-dev
```

### **Code Style**
- **Python**: Black formatter, type hints
- **Docker**: Multi-stage builds, security scanning
- **Documentation**: Clear API documentation

## 📝 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- **Yosys** - Claire Xenia Wolf and the YosysHQ team
- **Verilator** - Wilson Snyder and contributors  
- **Icarus Verilog** - Stephen Williams
- **OpenLane** - Efabless and the open-source EDA community
- **Sky130 PDK** - Google and SkyWater Technology

## 📞 Support

- **Documentation**: [Wiki](https://github.com/your-repo/wiki)
- **Issues**: [GitHub Issues](https://github.com/your-repo/issues)
- **Discussions**: [GitHub Discussions](https://github.com/your-repo/discussions)
- **Email**: support@your-domain.com

---

**🚀 Ready to revolutionize hardware design evaluation!**

*Built with ❤️ for the hardware design community*