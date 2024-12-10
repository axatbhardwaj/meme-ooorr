/*global describe, context, beforeEach, it*/
const { expect } = require("chai");
const { ethers } = require("hardhat");
const helpers = require("@nomicfoundation/hardhat-network-helpers");

// This works on a fork only!
const main = async () => {
    const fs = require("fs");
    const globalsFile = "globals.json";
    const dataFromJSON = fs.readFileSync(globalsFile, "utf8");
    const parsedData = JSON.parse(dataFromJSON);

    const AddressZero = ethers.constants.AddressZero;
    const HashZero = ethers.constants.HashZero;
    const name = "Meme";
    const symbol = "MM";
    const totalSupply = "1" + "0".repeat(24);
    const defaultDeposit = parsedData.minNativeTokenValue;//ethers.utils.parseEther("1");
    const defaultHash = "0x" + "5".repeat(64);
    const payload = "0x";
    const oneDay = 86400;
    const twoDays = 2 * oneDay;
    const nonce = 1;

    signers = await ethers.getSigners();
    deployer = signers[0];

    // UniswapPriceOracle
    const UniswapPriceOracle = await ethers.getContractFactory("UniswapPriceOracle");
    const uniswapPriceOracle = await UniswapPriceOracle.deploy(parsedData.wethAddress, parsedData.maxOracleSlippage,
        parsedData.pairAddress);
    await uniswapPriceOracle.deployed();

    // BuyBackBurnerUniswap implementation and proxy
    const BuyBackBurnerUniswap = await ethers.getContractFactory("BuyBackBurnerUniswap");
    const buyBackBurnerImplementation = await BuyBackBurnerUniswap.deploy();
    await buyBackBurnerImplementation.deployed();

    // Initialize buyBackBurner
    const proxyPayload = ethers.utils.defaultAbiCoder.encode(["address[]", "uint256"],
         [[parsedData.olasAddress, parsedData.wethAddress, uniswapPriceOracle.address,
         parsedData.routerV2Address], parsedData.maxBuyBackSlippage]);
    const proxyData = buyBackBurnerImplementation.interface.encodeFunctionData("initialize", [proxyPayload]);
    const BuyBackBurnerProxy = await ethers.getContractFactory("BuyBackBurnerProxy");
    const buyBackBurnerProxy = await BuyBackBurnerProxy.deploy(buyBackBurnerImplementation.address, proxyData);
    await buyBackBurnerProxy.deployed();

    const buyBackBurner = await ethers.getContractAt("BuyBackBurnerUniswap", buyBackBurnerProxy.address);

    const MemeEthereum = await ethers.getContractFactory("MemeEthereum");
    const memeEthereum = await MemeEthereum.deploy(parsedData.olasAddress, parsedData.wethAddress,
        parsedData.uniV3positionManagerAddress, buyBackBurner.address, parsedData.minNativeTokenValue);
    await memeEthereum.deployed();

    // Summon a new meme token
    await memeEthereum.summonThisMeme(name, symbol, totalSupply, {value: defaultDeposit});

    // Heart a new token by other accounts
    await memeEthereum.connect(signers[1]).heartThisMeme(nonce, {value: defaultDeposit});
    await memeEthereum.connect(signers[2]).heartThisMeme(nonce, {value: defaultDeposit});

    // Increase time to for 24 hours+
    await helpers.time.increase(oneDay + 100);

    // Unleash the meme token
    await memeEthereum.unleashThisMeme(nonce);

    const memeToken = await memeEthereum.memeTokens(0);
    console.log("New meme contract:", memeToken);

    // Collect by the first signer
    await memeEthereum.connect(signers[1]).collectThisMeme(memeToken);
};

main()
    .then(() => process.exit(0))
    .catch(error => {
        console.error(error);
        process.exit(1);
    });
