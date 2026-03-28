# TransferMaker

This application greatly simplifies the process of turning an image into a print for HTV transfers.

**Note: This is a work in progress! Use the issues for any bugs or feature request or discussions for any questions.**

## What Does This Do?

1. You give it an image 
2. You choose the colors you want and the output size and the minimum feature thickness
3. It will generate a set of images and you will choose the best one
4. It will upscale the image
4. It will clamp the colors to fit the ones you have chosen
5. It will vectorize the image
6. It will remove all features thinner than the minimum thickness you chosen
7. It will save the result as an svg file

What you do with it at that point is up to you.

## How Does This Work?

It uses a local AI inference engine which will download model weights to your computer and run an image editing AI model which processes the image initially. The rest of the processing is just regular boring computer code.

## Wait! Are you saying that you are using AI? I don't trust those AI companies!!

**No data leaves your computer at any time during this process!** The AI companies don't have any part in this. This is an open source model with a license that allows commercial use! You are good to go! But maybe read the license because I am not a lawyer and different countries have different rules. The model is named Flux 2 Klein 4B.

## What data are you collecting from me?

Nothing! You can uplug your router or turn off your wifi and it will still work! If you are tech savvy you can watch the network packets when it runs and except for files it needs to download the first time and checks for an update to the engine once on each start up you won't see the app talk to any computers outside of your network. 

## There has to be a catch!

There is, actually. You need a pretty beefy computer to run this, and an even beefier one to run it at any decent speed. Get an nvidia video card, 3000 series, 4000 series or 5000 series with at least 8GB of VRAM if you want to run it FAST. Otherwise you need at least 16GB of system RAM in your computer. Look, I know it sucks, but this thing is basically magic, so you can't really complain.

## Instructions

Click the green '<> Code' button towards the top of the page and select 'Download zip'. Extract the zip file to a directory and double click on 'run-windows.bat'. It will take a little while to download the AI and the inference engine (they are multiples of gigabytes), but you only have to do that once. When it is done loading just follow the directions in the UI window.

Don't forget to click on the star!

## Walkthrough

For this walkthrough I am using the [ED-209.](https://youtu.be/Hzlt7IbTp6M?t=41) along with a line spoken as part of its failure mode.

### Pre-Processing

First we have to find an image.

![Original](./media/ed-209.png)

Then pre-process it. I desaturated it and modified contrast, brightness, and levels, to get this:

![Pre-process](./media/ed-209-preprocessed.png)

### Run TransferMaker

Next we go to the TransferMaker directory and run 'run-windows.bat'.

After the models download and the engine is running it will start the interface.

### Steps

Choose your image and then choose the colors you want to use. The background needs to be a distinct color that won't be used -- it will be the t-shirt color. The minimum feature size is the thinnest segment in millimeters that you want to have. If you don't care, make it '0.1'. The special instructions are anything that you specifically want to have, like color choices "Make the hair red." or changing the image somehow "Close the man's eyes."

![Step1](./media/define_params.png)

Once the generations are complete we choose the one we want. We have an option to edit at this point, which will take the image we chose and put it through the model again with directions. Note that this has rapidly dimishing returns. 

![Step2](./media/pick_image.png)

In the next step the colors will be clamped. This means that the program conforms the colors in the image to the colors you specified in the beginning. 

![Step3](./media/clamp.png)

It will then split the clamped colors into layers and vectorize each individually.

The last step is removing the thin sections as specified earlier.

You can now save the completed SVG file and the intermediate layers.

![Step4](./media/check.png)

They should now be ready for printing. 

![Step5](./media/edit1.png)

Or, you can open it in an image editor and add things like text or alter whatever you need to before printing.

![Step6](./media/add_text.png)

Complete!

![Step7](./media/shirt.png)

## Acknowledgements

- [KoboldCPP](https://github.com/LostRuins/koboldcpp) for local AI processing