
from scanners.market_scanner import scanner

# Check what scan() actually does internally
import inspect
print(inspect.getsource(scanner.crypto_scanner.scan))