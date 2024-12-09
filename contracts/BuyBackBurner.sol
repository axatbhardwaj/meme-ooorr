// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {IUniswapV3} from "./interfaces/IUniswapV3.sol";
import {TickMath} from "./libraries/TickMath.sol";

/// @dev Only `owner` has a privilege, but the `sender` was provided.
/// @param sender Sender address.
/// @param owner Required sender address as an owner.
error OwnerOnly(address sender, address owner);

/// @dev Provided zero address.
error ZeroAddress();

/// @dev The contract is already initialized.
error AlreadyInitialized();

/// @title BuyBackBurner - BuyBackBurner implementation contract
abstract contract BuyBackBurner {
    event ImplementationUpdated(address indexed implementation);
    event OwnerUpdated(address indexed owner);
    event BridgeItLikeItsBurn(uint256 olasAmount);

    // Version number
    string public constant VERSION = "0.2.0";
    // Code position in storage is keccak256("BUY_BACK_BURNER_PROXY") = "c6d7bd4bd971fa336816fe30b665cc6caccce8b123cc8ea692d132f342c4fc19"
    bytes32 public constant BUY_BACK_BURNER_PROXY = 0xc6d7bd4bd971fa336816fe30b665cc6caccce8b123cc8ea692d132f342c4fc19;
    // L1 OLAS Burner address
    address public constant OLAS_BURNER = 0x51eb65012ca5cEB07320c497F4151aC207FEa4E0;
    // Max allowed price deviation for TWAP pool values (10%) in 1e18 format
    uint256 public constant MAX_ALLOWED_DEVIATION = 1e17;
    // Seconds ago to look back for TWAP pool values
    uint32 public constant SECONDS_AGO = 1800;

    // Reentrancy lock
    uint256 internal _locked = 1;
    // Contract owner
    address public owner;

    /// @dev Bridges OLAS amount back to L1 and burns.
    /// @param olasAmount OLAS amount.
    /// @param tokenGasLimit Token gas limit for bridging OLAS to L1.
    /// @param bridgePayload Optional additional bridge payload.
    /// @return leftovers msg.value leftovers if partially utilized by the bridge.
    function _bridgeAndBurn(
        uint256 olasAmount,
        uint256 tokenGasLimit,
        bytes memory bridgePayload
    ) internal virtual returns (uint256 leftovers);

    /// @dev Buys OLAS on DEX.
    /// @param nativeTokenAmount Suggested native token amount.
    /// @return olasAmount Obtained OLAS amount.
    function _buyOLAS(uint256 nativeTokenAmount) internal virtual returns (uint256 olasAmount);

    function _getTwapFromOracle(address pool) internal view returns (uint256 price) {
        // Query the pool for the current and historical tick
        uint32[] memory secondsAgos = new uint32[](2);
        // Start of the period
        secondsAgos[0] = SECONDS_AGO;

        // Fetch the tick cumulative values from the pool
        (int56[] memory tickCumulatives, ) = IUniswapV3(pool).observe(secondsAgos);

        // Calculate the average tick over the time period
        int56 tickCumulativeDelta = tickCumulatives[1] - tickCumulatives[0];
        int24 averageTick = int24(tickCumulativeDelta / int56(int32(SECONDS_AGO)));

        // Convert the average tick to sqrtPriceX96
        uint160 sqrtPriceX96 = TickMath.getSqrtRatioAtTick(averageTick);

        // Calculate the price using the sqrtPriceX96
        price = (uint256(sqrtPriceX96) * uint256(sqrtPriceX96)) / (1 << 192);
    }

    /// @dev BuyBackBurner initializer.
    /// @param payload Initializer payload.
    function _initialize(bytes memory payload) internal virtual;

    /// @dev BuyBackBurner initializer.
    /// @param payload Initializer payload.
    function initialize(bytes memory payload) external {
        if (owner != address(0)) {
            revert AlreadyInitialized();
        }

        owner = msg.sender;

        _initialize(payload);
    }

    /// @dev Changes the implementation contract address.
    /// @param newImplementation New implementation contract address.
    function changeImplementation(address newImplementation) external {
        // Check for the ownership
        if (msg.sender != owner) {
            revert OwnerOnly(msg.sender, owner);
        }

        // Check for zero address
        if (newImplementation == address(0)) {
            revert ZeroAddress();
        }

        // Store the implementation address
        assembly {
            sstore(BUY_BACK_BURNER_PROXY, newImplementation)
        }

        emit ImplementationUpdated(newImplementation);
    }

    /// @dev Changes contract owner address.
    /// @param newOwner Address of a new owner.
    function changeOwner(address newOwner) external virtual {
        // Check for the ownership
        if (msg.sender != owner) {
            revert OwnerOnly(msg.sender, owner);
        }

        // Check for the zero address
        if (newOwner == address(0)) {
            revert ZeroAddress();
        }

        owner = newOwner;
        emit OwnerUpdated(newOwner);
    }

    function checkPoolPrices(
        address token0,
        address token1,
        address uniV3PositionManager,
        uint24 fee
    ) external {
        // Get factory address
        address factory = IUniswapV3(uniV3PositionManager).factory();

        // Verify pool reserves before proceeding
        address pool = IUniswapV3(factory).getPool(token0, token1, fee);
        require(pool != address(0), "Pool does not exist");

        // Get current pool reserves
        (uint160 sqrtPriceX96, , uint16 observationCardinality, , , , ) = IUniswapV3(pool).slot0();
        // Check observation cardinality
        if (observationCardinality < 2) {
            // Increase observation cardinality to get more accurate twap
            IUniswapV3(pool).increaseObservationCardinalityNext(60);
            return;
        }

        // Check if the pool has sufficient observation history
        (uint32 oldestTimestamp, , , ) = IUniswapV3(pool).observations(0);
        if (oldestTimestamp + SECONDS_AGO >= block.timestamp) {
            return;
        }

        // Check TWAP or historical data
        uint256 twapPrice = _getTwapFromOracle(pool);
        // Get instant price
        uint256 instantPrice = (uint256(sqrtPriceX96) * uint256(sqrtPriceX96)) / (1 << 192);

        uint256 deviation;
        if (twapPrice > 0) {
            deviation = (instantPrice > twapPrice) ?
                ((instantPrice - twapPrice) * 1e18) / twapPrice :
                ((twapPrice - instantPrice) * 1e18) / twapPrice;
        }

        require(deviation <= MAX_ALLOWED_DEVIATION, "Price deviation too high");
    }

    /// @dev Bridges OLAS to Ethereum mainnet for burn.
    /// @notice if nativeTokenAmount is zero or above the balance, it will be adjusted to current native token balance.
    /// @param nativeTokenAmount Suggested native token amount.
    /// @param tokenGasLimit Token gas limit for bridging OLAS to L1.
    /// @param bridgePayload Optional additional bridge payload.
    function sendToHigherHeights(
        uint256 nativeTokenAmount,
        uint256 tokenGasLimit,
        bytes memory bridgePayload
    ) external payable {
        require(_locked == 1, "Reentrancy guard");
        _locked = 2;

        // Buy OLAS
        uint256 olasAmount = _buyOLAS(nativeTokenAmount);
        require(olasAmount > 0, "Nothing to bridge");

        // Burn OLAS
        uint256 leftovers = _bridgeAndBurn(olasAmount, tokenGasLimit, bridgePayload);

        // Send leftover amount, if any, back to the sender
        if (leftovers > 0) {
            // solhint-disable-next-line avoid-low-level-calls
            (bool success, ) = tx.origin.call{value: leftovers}("");
            require(success, "Leftovers transfer failed");
        }

        // TODO Check if needed
        // Update prices in oracle
        //IOracle(oracle).updatePrice();

        emit BridgeItLikeItsBurn(olasAmount);

        _locked = 1;
    }
}